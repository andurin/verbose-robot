#!/usr/bin/env python

import ujson as json
import logging
import zmq
import textwrap
from argparse import ArgumentParser
from argparse import RawDescriptionHelpFormatter
import multiprocessing
import os, traceback

from cifsdk_zmq import Client
from cif.constants import HUNTER_ADDR, ROUTER_ADDR, HUNTER_SINK_ADDR
from csirtg_indicator import Indicator
from cifsdk.utils import setup_runtime_path, setup_logging, get_argument_parser

logger = logging.getLogger(__name__)

SNDTIMEO = 15000
ZMQ_HWM = 1000000
EXCLUDE = os.environ.get('CIF_HUNTER_EXCLUDE', None)
HUNTER_ADVANCED = os.getenv('CIF_HUNTER_ADVANCED', 0)

TRACE = os.environ.get('CIF_HUNTER_TRACE', False)

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

if TRACE in [1, '1']:
    logger.setLevel(logging.DEBUG)


class Hunter(multiprocessing.Process):

    def __init__(self, remote=HUNTER_ADDR, router=ROUTER_ADDR, token=None):
        multiprocessing.Process.__init__(self)
        self.hunters = remote
        self.router = HUNTER_SINK_ADDR
        self.token = token
        self.exit = multiprocessing.Event()
        self.exclude = {}

        if EXCLUDE:
            for e in EXCLUDE.split(','):
                provider, tag = e.split(':')

                if not self.exclude.get(provider):
                    self.exclude[provider] = set()

                logger.debug('setting hunter to skip: {}/{}'.format(provider, tag))
                self.exclude[provider].add(tag)

    def _load_plugins(self):
        import pkgutil
        logger.debug('loading plugins...')
        plugins = []
        for loader, modname, is_pkg in pkgutil.iter_modules(cif.hunter.__path__, 'cif_hunter.'):
            p = loader.find_module(modname).load_module(modname)
            plugins.append(p)
            logger.debug('plugin loaded: {}'.format(modname))

        return plugins

    def terminate(self):
        self.exit.set()

    def start(self):
        router = Client(remote=self.router, token=self.token, nowait=True)
        plugins = self._load_plugins()
        socket = zmq.Context().socket(zmq.PULL)

        socket.SNDTIMEO = SNDTIMEO
        socket.set_hwm(ZMQ_HWM)

        logger.debug('connecting to {}'.format(self.hunters))
        socket.connect(self.hunters)
        logger.debug('starting hunter')

        poller = zmq.Poller()
        poller.register(socket, zmq.POLLIN)

        while not self.exit.is_set():
            try:
                s = dict(poller.poll(1000))
            except SystemExit or KeyboardInterrupt:
                break

            if socket not in s:
                continue

            data = socket.recv_multipart()

            logger.debug(data)
            data = json.loads(data[0])

            if isinstance(data, dict):
                if not data.get('indicator'):
                    continue

                if not data.get('itype'):
                    data = Indicator(
                        indicator=data['indicator'],
                        tags='search',
                        confidence=10,
                        group='everyone',
                        tlp='amber',
                    ).__dict__()

                if not data.get('tags'):
                    data['tags'] = []

                data = [data]

            for d in data:
                d = Indicator(**d)

                if d.indicator in ["", 'localhost', 'example.com']:
                    continue

                if self.exclude.get(d.provider):
                    for t in d.tags:
                        if t in self.exclude[d.provider]:
                            logger.debug('skipping: {}'.format(d.indicator))

                for p in plugins:
                    if p.is_advanced:
                        if not HUNTER_ADVANCED:
                            continue
                    try:
                        rv = p.process(d)
                        if rv and len(rv) > 0:
                            router.indicators_create(rv)
                    except Exception as e:
                        logger.error(e)
                        logger.error('[{}] giving up on: {}'.format(p, d))


def main():
    p = get_argument_parser()
    p = ArgumentParser(
        description=textwrap.dedent('''\
        Env Variables:
            
        example usage:
            $ cif-hunter -d
        '''),
        formatter_class=RawDescriptionHelpFormatter,
        prog='cif-router',
        parents=[p]
    )

    args = p.parse_args()
    setup_logging(args)
    logger = logging.getLogger(__name__)
    logger.info('loglevel is: {}'.format(logging.getLevelName(logger.getEffectiveLevel())))

    if args.logging_ignore:
        to_ignore = args.logging_ignore.split(',')

        for i in to_ignore:
            logging.getLogger(i).setLevel(logging.WARNING)

    setup_runtime_path(args.runtime_path)
    # setup_signals(__name__)

    h = Hunter()

    try:
        h.start()
    except KeyboardInterrupt:
        h.stop()


if __name__ == "__main__":
    main()