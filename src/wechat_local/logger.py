import logging


wxlog = logging.getLogger("wechat_local.database")
wxlog.addHandler(logging.NullHandler())
