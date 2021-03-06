import logging
import sys
import os

from logging.handlers import RotatingFileHandler


def initialize_logging(level=logging.INFO):
    if not os.path.exists("logs"):
        os.mkdir("logs")

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(logging.Formatter('%(levelname)s %(asctime)-15s %(name)s %(funcName)-8s %(message)s'))
    console.setLevel(level)

    file = RotatingFileHandler("logs/visiobas.log", maxBytes=10485760, backupCount=5)
    file.setFormatter(logging.Formatter('%(levelname)s %(asctime)-15s %(name)s %(funcName)-8s %(message)s'))
    file.setLevel(level)

    logging.root.setLevel(level)
    logging.root.addHandler(console)
    logging.root.addHandler(file)

    loggers = {
        'bacnet.parser': logging.getLogger("bacnet.parser"),
        'bacnet.slicer': logging.getLogger('bacnet.slicer'),
        'visiobas.data_collector': logging.getLogger('visiobas.data_collector'),
        'visiobas.data_collector.notifier': logging.getLogger('visiobas.data_collector.notifier'),
        'visiobas.data_collector.collector': logging.getLogger('visiobas.data_collector.collector'),
        'visiobas.data_collector.transmitter': logging.getLogger('visiobas.data_collector.transmitter')
    }
    # for name in loggers:
    #     logger = loggers[name]
    #     logger.addHandler(console)
    #     logger.addHandler(file)
    #     logger.setLevel(level)

    # loggers['bacnet.parser'].setLevel(logging.DEBUG)
    # loggers['bacnet.slicer'].setLevel(logging.DEBUG)
    # loggers['visiobas.data_collector'].setLevel(logging.INFO)
    # loggers['visiobas.data_collector.notifier'].setLevel(logging.INFO)
    # loggers['visiobas.data_collector.collector'].setLevel(logging.INFO)
    # loggers['visiobas.data_collector.transmitter'].setLevel(logging.INFO)
