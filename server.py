from firebase_admin import credentials, firestore, initialize_app, db
import firebase_admin
import random
from flask import Flask, make_response, redirect, url_for, session
from flask import abort, request, jsonify
import json
from datetime import datetime
import faiss 

# import tensorflow as tf
# gpus = tf.config.experimental.list_physical_devices('GPU')
# for gpu in gpus:
#     tf.config.experimental.set_memory_growth(gpu, True)
from flask import Flask
from flask_cors import CORS
import logging
import threading

from multiprocessing import Lock

import sys
sys.path.append("./")
sys.path.append("../")

import pandas as pd
from sentence_transformers import SentenceTransformer
import openai
from passwords import open_ai_api_key
openai.api_key = open_ai_api_key

from numpy import dot
from numpy.linalg import norm
import numpy as np

import torch
from model.EmpathicSimilarityModel import EmpathicSimilarityModel
from model.EmpathicSummaryModel import EmpathicSummaryModel
from model.EmpathicMultitaskModel import EmpathicMultitaskModel
from omegaconf import OmegaConf

hp_BART = OmegaConf.load("../config/BART_joint_avg_pretraining.yaml")
hp_SBERT_finetuned = OmegaConf.load("../config/SBERT_similarity_avg_mse.yaml")

df_clean = pd.read_csv("./STORIES (user study).csv")
df_clean =  df_clean.loc[:, ~df_clean.columns.str.contains('^Unnamed')]

model_BART = EmpathicMultitaskModel.load_from_checkpoint("../lightning_logs/BART_joint_avg_encoder/checkpoints/epoch=12-step=650.ckpt", hp=hp_BART)
modeL_SBERT_finetuned = EmpathicSimilarityModel.load_from_checkpoint("../lightning_logs/SBERT_similarity_avg/checkpoints/epoch=8-step=225.ckpt", hp=hp_SBERT_finetuned)
model_SBERT = SentenceTransformer('all-mpnet-base-v2', device='cpu')
df_clean = pd.read_csv("STORIES (user study).csv")
# df_clean["embeddings_SBERT"] = df_clean["embeddings_SBERT"].apply(eval)

embeddings_sbert = np.array([list(eval(_)) for _ in df_clean["embedding (SBERT, no finetuning)"]], dtype=np.float32)
d = len(embeddings_sbert[0])
index_sbert = faiss.index_factory(d, "Flat", faiss.METRIC_INNER_PRODUCT)
faiss.normalize_L2(embeddings_sbert)
index_sbert.add(embeddings_sbert)

embeddings_bart = np.array([list(eval(_)) for _ in df_clean['embedding (BART, finetuning, joint, encoder_hidden_state, mse)']], dtype=np.float32)
d = len(embeddings_bart[0])
index_bart = faiss.index_factory(d, "Flat", faiss.METRIC_INNER_PRODUCT)
faiss.normalize_L2(embeddings_bart)
index_bart.add(embeddings_bart)

embeddings_sbert_finetuned = np.array([list(eval(_)) for _ in df_clean['embedding (SBERT, finetuning, mse)']], dtype=np.float32)
d = len(embeddings_sbert_finetuned[0])
index_sbert_finetuned = faiss.index_factory(d, "Flat", faiss.METRIC_INNER_PRODUCT)
faiss.normalize_L2(embeddings_sbert_finetuned)
index_sbert_finetuned.add(embeddings_sbert_finetuned)

lock = Lock()
app = Flask(__name__)
CORS(app)
logging.getLogger('flask_cors').level = logging.DEBUG

sem = threading.Semaphore()

c = firebase_admin.credentials.Certificate("./credentials.json")
default_app = firebase_admin.initialize_app(c, {
    'databaseURL': "https://empathic-stories-default-rtdb.firebaseio.com/"
})



ALLOWED_USERS = {'p001', 'p002', 'p000', 'p003', 'p004', 'p005', 'p006', 'p007', 'p008', 'p009', 'p010', 'p011', 'p012', 'p013', 'p014'}


def get_cosine_similarity(a, b):
    cos_sim = dot(a, b)/(norm(a)*norm(b))
    return cos_sim

def retrieve_top(embedding, model = "SBERT", k=len(df_clean)): # embedding must be (1, 768)
    #Using FAISS
    embedding = embedding.reshape(1, 768)
    faiss.normalize_L2(embedding)

    if model == "SBERT":
        D, I = index_sbert.search(embedding, k) 
    elif model == "SBERT_finetuned":
        D, I = index_sbert_finetuned.search(embedding, k)
    else:
        D, I = index_bart.search(embedding, k)
    return D, I

@app.route('/')
def hello_world():
    return "Hello World"


@app.route('/participantIDInput/', methods=["GET", "POST"])
def get_participant_id():
    sem.acquire()
    # """Get current session number for participant"""
    # # randomly select story FROM stories that haven't been seen before (store it in firebase)
    # print('test')
    participantIDInput = request.json['participantIDInput']
    print(f'The value of my id is {participantIDInput}')
    if participantIDInput not in ALLOWED_USERS:
        sem.release()
        abort(404)
    ref = db.reference(participantIDInput)
    currentSession = db.reference(participantIDInput + "/currentSession").get()

    if currentSession is None:
        db.reference(participantIDInput + "/currentSession").set(1)
    elif currentSession not in [1, 2, 3]:
        sem.release()
        abort(404)   
    sem.release()
    return "success"

def get_tokens(x, model="BART"):
    if model == "BART":
        tokens = tokenizer_BART(
            x, 
            return_tensors = "pt",
            truncation=True, 
            padding="max_length",
            return_attention_mask = True,
            # return_special_tokens_mask = True,
            max_length=tokenizer_BART.model_max_length
        )
    else:
        tokens = tokenizer_SBERT(x)
    return tokens

def get_stories_from_model(mystory, top_k = 1):
    prompt = f"""Story: 
    {mystory}
    Write a story from your own life that the narrator would empathize with. Do not refer to the narrator explicitly.
    """
    mystory = mystory.replace("\n", "")

    with torch.no_grad():
        embedding_sbert = model_SBERT.encode(mystory)
        # embedding_sbert_finetuned = modeL_SBERT_finetuned.model.encode(mystory)
        embedding_bart, _ = model_BART.get_embedding_and_summaries(get_tokens(mystory))
        embedding_bart = embedding_bart.detach().numpy()
        r1 = list(df_clean["story_formatted"].iloc[retrieve_top(embedding_bart, model="BART")[1][0][0:top_k]])
        r2 = list(df_clean["story_formatted"].iloc[retrieve_top(embedding_sbert)[1][0][0:top_k]])
        # r4 = list(df_clean["story_formatted"].iloc[retrieve_top(embedding_sbert_finetuned)[1][0][0:top_k]])


    response = openai.ChatCompletion.create(
        model="gpt-3.5-turbo",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=500
    )
    r3 = response["choices"][0]["message"]["content"].replace("\n\n", "\n")
    return {"condition1": r1, "condition2": r2, "condition3": r3}


@app.route('/sessionDone/', methods=["GET", "POST"])
def sessionDone():
    sem.acquire()
    id = request.json['participantIDInput']
    ref = db.reference(id)
    currentSession = db.reference(id + "/currentSession").get()

    dict = {'showParticipantID': id, 'showSessionNum': currentSession}
    sem.release()
    return json.dumps(dict)
            

@app.route('/getPrompt/', methods=["GET", "POST"])
def getPrompt():
    # """Get initial writing prompt for user + retrieve 3 stories from 3 models + save to firebase"""
    sem.acquire()
    id = request.json['participantIDInput']
    ##########################################################################
    currentSession = db.reference(id + "/currentSession").get()
    dict = {'showParticipantID': id, 'showSessionNum': currentSession}
    session = db.reference(id + '/s00' + str(currentSession))
    session.child("startTime").set(datetime.now().strftime("%m/%d/%Y, %H:%M:%S"))
    sem.release()
    return json.dumps(dict)


@app.route('/submitMyStory/', methods=["GET", "POST"])
def submitMyStory():
    """Save their story in firebase"""
    sem.acquire()
    id = request.json['participantIDInput']
    currentSession = db.reference(id + "/currentSession").get()
    if currentSession == 1:
        gender = request.json['gender']
        age = request.json['age']
        race = request.json['race']
        empathyLevel = request.json['empathyLevel']
        demographic = {"gender": gender, "age": age,
                        "race": race, "empathyLevel": empathyLevel}

    valence = request.json['valence']
    arousal = request.json['arousal']
    reflection = {"valence": valence, "arousal": arousal}
    mystory = request.json['mystory']
    fullDate = request.json['fullDate']
    mystoryTopic = request.json['mystoryTopic']
    mainEvent = request.json['mainEvent']
    narratorEmotions = request.json['narratorEmotions']
    moral = request.json['moral']

    mystoryQuestions = {"mainEvent": mainEvent,
                    "narratorEmotions": narratorEmotions, "moral": moral, "fullDate": fullDate}
    ref = db.reference(id)
    session = db.reference(id + '/s00' + str(currentSession))

    session.child("mystory").set(mystory)
    session.child("mystoryTopic").set(mystoryTopic)
    session.child("mystoryQuestions").set(mystoryQuestions)
    session.child("reflection").set(reflection)
    session.child("submitStoryTime").set(datetime.now().strftime("%m/%d/%Y, %H:%M:%S"))
    if currentSession == 1:
        session.child("demographic").set(demographic) 


    ## make call to model and save firebase mapping
    stories = get_stories_from_model(mystory)

    session.child("storyMap").set(stories)
    # TODO: remove duplicates and randomize, save to firebase with what model it came from, send the 1-4 stories back to frontend to display

    dict = {
        'mystory': mystory
    }
    # check for duplicates
    unique_stories = list(set(stories.values()))
    random.shuffle(unique_stories)
    dict['numOfStories'] = len(unique_stories)
    for i in range(len(unique_stories)):
        dict["story" + str(i + 1)] = unique_stories[i]
    session.child("randomizedStories").set(dict)
    sem.release()
    return json.dumps(dict)

@app.route('/submitSurveyQuestions/', methods=["GET", "POST"])
def submitSurveyQuestions():
    sem.acquire()
    id = request.json['participantIDInput']
    ref = db.reference(id)
    currentSession = db.reference(id + "/currentSession").get()
    print(currentSession)
    if currentSession == 3:
        print("i will take the new value of part 6 now")
        empathyWithAI = request.json['empathyWithAI']

    survey1_answers = request.json['survey1_answers']
    survey2_answers = request.json['survey2_answers']
    survey3_answers = request.json['survey3_answers']
    # survey4_answers = request.json['survey4_answers']
    mostEmpathizedOrder = request.json['mostEmpathizedOrder']
    
    feedback = request.json['feedback']

    # ref = db.reference(id)
    # currentSession = db.reference(id + "/currentSession").get()
    session = db.reference(id + '/s00' + str(currentSession))
    session.child("endTime").set(datetime.now().strftime("%m/%d/%Y, %H:%M:%S"))
    if currentSession == 1:
        session1 = db.reference(id + '/s001')
        session1.child("feedback").set(feedback)
        # session1.child("prompt").set(dbprompt1)
        session1.child("survey1_answers").set(survey1_answers)
        session1.child("survey2_answers").set(survey2_answers)
        session1.child("survey3_answers").set(survey3_answers)
        session1.child("mostEmpathizedOrder").set(mostEmpathizedOrder)
        db.reference(id + "/currentSession").set(2)

    elif currentSession == 2:
        session2 = db.reference(id + '/s002')
        # session2.child("prompt").set(dbprompt2)
        session2.child("feedback").set(feedback)
        session2.child("survey1_answers").set(survey1_answers)
        session2.child("survey2_answers").set(survey2_answers)
        session2.child("survey3_answers").set(survey3_answers)
        session2.child("mostEmpathizedOrder").set(mostEmpathizedOrder)
        db.reference(id + "/currentSession").set(3)

    elif currentSession == 3:
        print("i will submit everything now")
        session3 = db.reference(id + '/s003')
        # session3.child("prompt").set(prompt3)
        session3.child("feedback").set(feedback)
        session3.child("survey1_answers").set(survey1_answers)
        session3.child("survey2_answers").set(survey2_answers)
        session3.child("survey3_answers").set(survey3_answers)
        session3.child("mostEmpathizedOrder").set(mostEmpathizedOrder)
        session3.child("empathyWithAI").set(empathyWithAI)
        db.reference(id + "/currentSession").set(4)
    sem.release()
    return 'Data submitted successfully!'

################################### START SERVER ###################################
# to run the server run the following command:
# python server.py '0.0.0.0' 5192 1
if __name__ == '__main__':
    host = sys.argv[1]
    port = sys.argv[2]
    debug = sys.argv[3]
    # app.run(host='0.0.0.0', port=5000, debug=True)
    app.run(host=host, port=port, debug=debug, ssl_context=("/etc/letsencrypt/live/matlaberp8.media.mit.edu/fullchain.pem", "/etc/letsencrypt/live/matlaberp8.media.mit.edu/privkey.pem"))
