"""Import the standalone adapter without loading Cura; optional real Qt widgets."""
import importlib.util
import sys
import types
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "cura_extension" / "E3PBlenderMold"
NAMESPACE = "_e3p_cura_test"


def load_worker():
    if NAMESPACE not in sys.modules:
        namespace = types.ModuleType(NAMESPACE)
        namespace.__path__ = [str(PLUGIN)]
        sys.modules[NAMESPACE] = namespace
    return __import__(NAMESPACE + ".worker", fromlist=["worker"])


def load_installer():
    spec = importlib.util.spec_from_file_location("_e3p_installer", ROOT / "tools" / "install_cura_extension.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_ui():
    load_worker()
    class Signal:
        def __init__(self):
            self.slots = []

        def connect(self, slot):
            self.slots.append(slot)

    class Job:
        def __init__(self):
            self.finished = Signal()
            self.started = False

        def start(self):
            self.started = True

    class Extension:
        def setMenuName(self, _name):
            pass

        def addMenuItem(self, _name, _callback):
            pass

    class Preferences:
        def __init__(self):
            self.values = {}

        def addPreference(self, name, value):
            self.values.setdefault(name, value)

        def getValue(self, name):
            return self.values.get(name)

        def setValue(self, name, value):
            self.values[name] = value

    class Application:
        preferences = Preferences()
        imports = []

        @classmethod
        def getInstance(cls):
            return cls()

        def getPreferences(self):
            return self.preferences

        def readLocalFile(self, url, **_kwargs):
            self.imports.append(url.toLocalFile())

    class Message:
        MessageType = types.SimpleNamespace(ERROR=1, NEUTRAL=0)
        messages = []

        def __init__(self, **kwargs):
            self.kwargs, self.hidden = kwargs, False
            self.messages.append(self)

        def show(self):
            pass

        def hide(self):
            self.hidden = True

    attributes = {
        "UM.Application": {"Application": Application},
        "UM.Extension": {"Extension": Extension},
        "UM.Job": {"Job": Job},
        "UM.Logger": {"Logger": types.SimpleNamespace(log=lambda *_: None, logException=lambda *_: None)},
        "UM.Message": {"Message": Message},
        "UM.Scene.Iterator.DepthFirstIterator": {"DepthFirstIterator": lambda _: []},
        "UM.Scene.Selection": {"Selection": types.SimpleNamespace(getAllSelectedObjects=lambda: [])},
        "UM.i18n": {"i18nCatalog": lambda _: types.SimpleNamespace(i18nc=lambda _, message: message)},
        "cura.CuraApplication": {"CuraApplication": Application},
        "cura.Scene.CuraSceneNode": {"CuraSceneNode": type("CuraSceneNode", (), {})},
    }
    modules = {}
    for name, values in attributes.items():
        module = types.ModuleType(name)
        module.__dict__.update(values)
        modules[name] = module
    name = NAMESPACE + ".E3PBlenderMold"
    spec = importlib.util.spec_from_file_location(name, PLUGIN / "E3PBlenderMold.py")
    module = importlib.util.module_from_spec(spec)
    # dataclasses needs the defining module while processing its annotations.
    sys.modules[name] = module
    with patch.dict(sys.modules, modules):
        spec.loader.exec_module(module)
    return module
