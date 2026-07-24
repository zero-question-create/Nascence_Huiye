from PyQt5.QtCore import QObject, pyqtSignal


class EventBus(QObject):
    log = pyqtSignal(str, str)
    status = pyqtSignal(str)
    chat_reply = pyqtSignal(str, str)
    task_error = pyqtSignal(str)
    message = pyqtSignal(str, str, str)


BUS = EventBus()
