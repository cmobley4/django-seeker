from django.apps import apps, AppConfig
from .mapping import Indexable, ModelIndex
import threading

documents = []

model_documents = {}
model_doc_types = {}
app_documents = {}

current_app = threading.local()

def register(doc_class, app=None):
    global current_app
    assert issubclass(doc_class, Indexable)
    if app:
        assert isinstance(app, AppConfig) and apps.is_installed(app.name)
    if doc_class in documents:
        return
    documents.append(doc_class)
    if issubclass(doc_class, ModelIndex):
        model_class = doc_class.queryset().model
        # It's possible to register more than one document type for a model, so keep a list.
        model_documents.setdefault(model_class, []).append(doc_class)
        # For doing queries across multiple document types, we'll need a mapping from doc_type back to model_class.
        model_doc_types[doc_class._doc_type.name] = model_class
    try:
        app = app if app else current_app
        app_documents.setdefault(app.label, []).append(doc_class)
    except:
        pass
