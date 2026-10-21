# Flask на хостинге airlok: gunicorn airlok поставит сам.
from flask import Flask

app = Flask(__name__)


@app.get("/")
def index():
    return "<h1>Flask на хостинге airlok</h1>"
