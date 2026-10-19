from . import E3PBlenderMold


def getMetaData():
    return {}


def register(app):
    return {"extension": E3PBlenderMold.E3PBlenderMold()}
