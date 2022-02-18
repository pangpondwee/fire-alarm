from fastapi import FastAPI, HTTPException, Depends, status
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from jose import JWTError, jwt
from passlib.context import CryptContext
from pydantic import BaseModel
from typing import Optional
from datetime import datetime, timedelta
from fastapi.middleware.cors import CORSMiddleware
from pymongo import MongoClient
import requests

SECRET_KEY = "09d25e094faa6ca2556c818166b7a9563b93f7099f6f0f4caa6cf63b88e8d3e7"
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 30

app = FastAPI()

origins = ["*"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

client = MongoClient('mongodb://localhost', 27017)
db = client["fire-alarm"]

menu_collection = db['record']
avg_collection = db['record_avg']
configure_collection = db['configure']


class Token(BaseModel):
    access_token: str
    token_type: str


class TokenData(BaseModel):
    username: Optional[str] = None


class User(BaseModel):
    username: str
    email: Optional[str] = None
    full_name: Optional[str] = None
    disabled: Optional[bool] = None


class UserInDB(User):
    hashed_password: str


class UserRegistration(BaseModel):
    username: str
    email: Optional[str] = None
    full_name: Optional[str] = None
    password: str


class Alarm(BaseModel):
    number: int
    gas: int
    flame1: int
    flame2: int
    flame3: int
    temp1: int
    temp2: int
    temp3: int


pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/users/login")


@app.get("/fire-alarm/get-record") #get-frontend
def get_fire_record():
    room = avg_collection.find()
    result = []
    #print('room:', room)
    for r in room:
        ref = configure_collection.find_one({"number":r['number']})
        result.append(
            {   'number': r['number'],
                'current_flame': sum(r['flame']) / len(r['flame']), 
                'current_gas': r['gas'],
                'current_temp': sum(r['temp']) / len(r['temp']),
                'ref_flame': ref['ref_flame'],
                'ref_gas': ref['ref_gas'],
                'ref_temp': ref['ref_temp']})  
                # default ref temp 50-58, ref gas 2000-5000
        #print(r)
    return {'Room': result}  # result


@app.get("/fire-alarm/alarm")  # get-hardware
def alarm():
    alarm = avg_collection.find_one({'number': 1}, {'_id': 0})  # search number:1
    if alarm:
        ref = configure_collection.find_one({'number': alarm['number']}, {'_id': 0})
        flame = 1 if sum(alarm['flame']) < ref['ref_flame'] else 0
        gas = 1 if alarm['gas'] > ref['ref_gas'] else 0
        temp = 1 if sum(alarm['temp']) > ref['ref_temp'] else 0
        return {'flame': flame, 'gas': gas, 'temp': temp}
    else:
        raise HTTPException(404, "Not have data of this number.")


def line_notify(alarm: dict):
    ref = configure_collection.find_one({'number': alarm['number']}, {'_id': 0})
    if ref['line_token']  and ref['notification'] == True:  # user set line_token and notification is on
        if (alarm['flame'] > ref['ref_flame'] or
                alarm['gas'] > ref['ref_gas'] or alarm['temp'] > ref['ref_temp']):
            msg = "Warning!!\n"
            if ref['address'] is not None:
                msg += f"At {ref['address']}\n"
            if alarm['flame'] < ref['ref_flame']:
                msg += f"Flame less than {ref['ref_flame']}\n"
            if alarm['gas'] > ref['ref_gas']:
                msg += f"Gas over {ref['ref_gas']}\n"
            if alarm['temp'] > ref['ref_temp']:
                msg += f"Temp over {ref['ref_temp']}\n"

            url = 'https://notify-api.line.me/api/notify'
            headers = {'content-type': 'application/x-www-form-urlencoded',
                       'Authorization': 'Bearer ' + ref['line_token']}
            requests.post(url, headers=headers, data={'message': msg})


@app.post("/fire-alarm/update")  # post-hardware
def update(alarm: Alarm):
    # add new record
    list_flame = [alarm.flame1, alarm.flame2, alarm.flame3]
    list_temp = [alarm.temp1, alarm.temp2, alarm.temp3]
    alarm_dict = {'number': alarm.number, 'flame': list_flame,
                  'gas': alarm.gas, 'temp': list_temp,
                  'update_time': datetime.now().strftime('%Y-%m-%dT%H:%M:%S.%f%z')}
    menu_collection.insert_one(alarm_dict)

    # delete old record
    lst = list(menu_collection.find({'number': alarm.number}, {'_id': 0}))
    lst.sort(key=lambda x: x['update_time'])  # sort by time for delete excess data
    for i in range(len(lst) - 3):
        menu_collection.delete_one(lst[i])  # delete in db if more than 3

    # add default configure_collection
    chk = configure_collection.find_one({'number': alarm.number}, {'_id': 0})  # check data in configure_collection
    if not chk:
        default_dict = {'number': alarm.number, 'address': None, 'line_token': None,
                        'ref_flame': 500, 'ref_gas': 2000, 'ref_temp': 50,
                        'notification': True, 'update_time': datetime.now().strftime('%Y-%m-%dT%H:%M:%S.%f%z')}
        configure_collection.insert_one(default_dict)

    # update record_avg
    chk = avg_collection.find_one({'number': alarm.number}, {'_id': 0})  # check data in avg_collection
    if chk == None:  # if not have data -> add new
        avg_collection.insert_one(alarm_dict)
    else:  # if have date -> update
        # calculate average record
        avg_flame = []
        avg_temp = []
        lst = list(menu_collection.find({'number': alarm.number}, {'_id': 0}))
        for i in range(len(lst)):
            avg_flame.append(sum(d['flame'][i] for d in lst) / len(lst))
            avg_temp.append(sum(d['temp'][i] for d in lst) / len(lst))
        avg_gas = sum(d['gas'] for d in lst) / len(lst)

        # update
        query = {"number": alarm.number}
        new = {"$set": {"flame": avg_flame, "temp": avg_temp,
                        "gas": avg_gas, 'update_time': datetime.now().strftime('%Y-%m-%dT%H:%M:%S.%f%z')}}
        avg_collection.update_one(query, new)
    
        # line notify
        res = avg_collection.find_one({"number": alarm.number})
        res["flame"] = sum(avg_flame) / len(avg_flame)
        res["temp"] = sum(avg_temp) / len(avg_temp)
        line_notify(res)

    return "Update completed."


def verify_password(plain_password, hashed_password):
    return pwd_context.verify(plain_password, hashed_password)


def get_password_hash(password):
    return pwd_context.hash(password)


def get_user(username: str):
    users = db['users'].find({}, {"_id": 0})
    database = {}
    for i in users:
        database[f"{i['username']}"] = i
    print(database)

    if username in database:
        user_dict = database[username]
        return UserInDB(**user_dict)


def authenticate_user(username: str, password: str):
    user = get_user(username)
    if not user:
        return False
    if not verify_password(password, user.hashed_password):
        return False
    return user


def create_access_token(data: dict, expires_delta: Optional[timedelta] = None):
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.utcnow() + expires_delta
    else:
        expire = datetime.utcnow() + timedelta(minutes=15)
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt


async def get_current_user(token: str = Depends(oauth2_scheme)):
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username: str = payload.get("sub")
        if username is None:
            raise credentials_exception
        token_data = TokenData(username=username)
    except JWTError:
        raise credentials_exception
    user = get_user(username=token_data.username)
    if user is None:
        raise credentials_exception
    return user


async def get_current_active_user(current_user: User = Depends(get_current_user)):
    if current_user.disabled:
        raise HTTPException(status_code=400, detail="Inactive user")
    return current_user


@app.post("/users/login", response_model=Token)
async def login_for_access_token(form_data: OAuth2PasswordRequestForm = Depends()):
    user = authenticate_user(form_data.username, form_data.password)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    access_token_expires = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    access_token = create_access_token(
        data={"sub": user.username}, expires_delta=access_token_expires
    )
    return {"access_token": access_token, "token_type": "bearer"}


@app.post('/users/register')
async def create_user(user: UserRegistration):
    # add event not create if username already exist
    print(user)
    registration_user = {"username": user.username, "full_name": user.full_name, "email": user.email,
                         "hashed_password": get_password_hash(user.password), "disabled": False}
    print(registration_user)
    db['users'].insert_one(registration_user)


@app.get("/users/me/", response_model=User)
async def read_users_me(current_user: User = Depends(get_current_active_user)):
    return current_user


@app.get("/users/me/items/")
async def read_own_items(current_user: User = Depends(get_current_active_user)):
    return [{"item_id": "Foo", "owner": current_user.username}]
