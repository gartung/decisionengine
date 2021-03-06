#!/usr/bin/env python3
"""
Main loop for Decision Engine.
The following environment variable points to decision engine configuration file:
``DECISION_ENGINE_CONFIG_FILE``
if this environment variable is not defined the ``DE-Config.py`` file from the ``../tests/etc/` directory will be used.
"""

import argparse
import importlib
import logging
import signal
import sys
import multiprocessing
import pandas as pd
import os
import tabulate
import time
import uuid

try:
    import SocketServer
except ImportError:
    import socketserver as SocketServer
    pass

try:
    import SimpleXMLRPCServer
except ImportError:
    import xmlrpc.server as SimpleXMLRPCServer
    pass


import decisionengine.framework.configmanager.ConfigManager as Conf_Manager
import decisionengine.framework.dataspace.datablock as datablock
import decisionengine.framework.dataspace.dataspace as dataspace
import decisionengine.framework.taskmanager.TaskManager as TaskManager

CONFIG_UPDATE_PERIOD = 10  # seconds
FORMATTER = logging.Formatter(
    "%(asctime)s - %(name)s - %(module)s - %(process)d - %(threadName)s - %(levelname)s - %(message)s")
LOG_LEVELS_DICT = {0: 'NOTSET', 10: 'DEBUG', 20: 'INFO', 30: 'WARNING', 40: 'ERROR', 50: 'CRITICAL'}

class Worker(multiprocessing.Process):

    def __init__(self, task_manager, config):
        super().__init__()
        self.task_manager = task_manager
        self.logger = None
        self.config = config

    def run(self):
        self.logger = logging.getLogger()
        file_handler = logging.handlers.RotatingFileHandler(os.path.join(
                                                            os.path.dirname(
                                                                self.config["logger"]["log_file"]),
                                                            self.task_manager.name + ".log"),
                                                            maxBytes=self.config["logger"].get("max_file_size",
                                                                                               200 * 1000000),
                                                            backupCount=self.config["logger"].get("max_backup_count",
                                                                                                  6))
        file_handler.setFormatter(FORMATTER)
        self.logger.setLevel(logging.WARNING)
        self.logger.addHandler(file_handler)
        channel_log_level = self.config["logger"].get("global_channel_log_level", "WARNING")
        self.task_manager.set_loglevel(TaskManager.LOG_LEVELS_DICT[channel_log_level])
        self.task_manager.run()


class RequestHandler(SimpleXMLRPCServer.SimpleXMLRPCRequestHandler):
    rpc_paths = ('/RPC2',)


class RpcServer(SocketServer.ThreadingMixIn, SimpleXMLRPCServer.SimpleXMLRPCServer):
    def __init__(self, server_address, RequestHandlerClass):
        SimpleXMLRPCServer.SimpleXMLRPCServer.__init__(
            self, server_address, requestHandler=RequestHandlerClass)


class DecisionEngine(SocketServer.ThreadingMixIn,
                     SimpleXMLRPCServer.SimpleXMLRPCServer):

    def __init__(self, cfg, server_address, RequestHandlerClass):
        SimpleXMLRPCServer.SimpleXMLRPCServer.__init__(self,
                                                       server_address,
                                                       logRequests=False,
                                                       requestHandler=RequestHandlerClass)

        self.logger = logging.getLogger("decision_engine")
        signal.signal(signal.SIGHUP, self.handle_sighup)
        self.task_managers = {}
        self.config_manager = cfg
        self.dataspace = dataspace.DataSpace(self.config_manager.get_global_config())
        self.reaper = dataspace.Reaper(self.config_manager.get_global_config())
        self.logger.info("DecisionEngine started on {}".format(server_address))

    def get_logger(self):
        return self.logger

    def _dispatch(self, method, params):
        try:
            """
            methods allowed to be executed by rpc
            have rpc pre-pended
            """
            func = getattr(self, "rpc_" + method)
        except AttributeError:
            raise Exception('method "%s" is not supported' % method)
        else:
            return func(*params)

    def rpc_show_config(self, channel):
        """

        :type channel: string
        """
        txt = ""
        channels = self.config_manager.get_channels()
        if channel == 'all':
            for ch in channels:
                txt += "Channel: {} ".format(ch)
                txt += "Config: {} ".format(channels[ch])
        else:
            txt += "Channel: {} ".format(channel)
            txt += "Config: {} ".format(channels[channel])
        return txt[:-1]

    def rpc_show_de_config(self):
        config = self.config_manager.get_global_config()
        txt = ""
        for k in config:
            txt += "{}".format(config[k])
        return txt[:-1]

    def rpc_print_product(self, product, columns=None, query=None):
        found = False
        txt = "Product {}: ".format(product)
        for ch, worker in list(self.task_managers.items()):
            channel_config = self.config_manager.get_channels()[ch]
            produces = self.config_manager.get_produces(channel_config)
            r = [x for x in list(produces.items()) if product in x[1]]
            if not r:
                continue
            found = True
            txt += " Found in channel {}\n".format(ch)
            tm = self.dataspace.get_taskmanager(ch)
            try:
                data_block = datablock.DataBlock(self.dataspace,
                                                 ch,
                                                 taskmanager_id=tm['taskmanager_id'],
                                                 sequence_id=tm['sequence_id'])
                data_block.generation_id -= 1
                df = data_block[product]
                df = pd.read_json(df.to_json())
                column_names = []
                if columns:
                    column_names = columns.split(",")
                if query:
                    if column_names:
                        txt += "{}\n".format(tabulate.tabulate(df.loc[:, column_names].query(query),
                                                               headers='keys',
                                                               tablefmt='psql'))
                    else:
                        txt += "{}\n".format(tabulate.tabulate(df.query(query),
                                                               headers='keys',
                                                               tablefmt='psql'))

                else:
                    if column_names:
                        txt += "{}\n".format(tabulate.tabulate(df.loc[:, column_names],
                                                               headers='keys',
                                                               tablefmt='psql'))
                    else:
                        txt += "{}\n".format(tabulate.tabulate(df,
                                                               headers='keys',
                                                               tablefmt='psql'))
            except Exception as e:
                txt += "\t\t{}\n".format(e)
            if not found:
                txt += "Not Found\n"
        return txt[:-1]

    def rpc_print_products(self):
        width = max([len(x) for x in list(self.task_managers.keys())]) + 1
        txt = ""
        for ch, worker in list(self.task_managers.items()):
            sname = TaskManager.STATE_NAMES[worker.task_manager.get_state()]
            txt += "channel: {:<{width}}, id = {:<{width}}, state = {:<10} \n".format(ch,
                                                                                      worker.task_manager.id,
                                                                                      sname,
                                                                                      width=width)
            tm = self.dataspace.get_taskmanager(ch)
            data_block = datablock.DataBlock(self.dataspace,
                                             ch,
                                             taskmanager_id=tm['taskmanager_id'],
                                             sequence_id=tm['sequence_id'])
            data_block.generation_id -= 1
            channel_config = self.config_manager.get_channels()[ch]
            produces = self.config_manager.get_produces(channel_config)
            for i in ("sources",
                      "transforms",
                      "logicengines",
                      "publishers"):
                txt += "\t{}:\n".format(i)
                modules = channel_config.get(i, {})
                for mod_name, mod_config in modules.items():
                    txt += "\t\t{}\n".format(mod_name)
                    products = produces.get(mod_name, [])
                    for product in products:
                        try:
                            df = data_block[product]
                            df = pd.read_json(df.to_json())
                            txt += "{}\n".format(tabulate.tabulate(df,
                                                                   headers='keys', tablefmt='psql'))
                        except Exception as e:
                            txt += "\t\t\t{}\n".format(e)
        return txt[:-1]

    def rpc_status(self):
        width = max([len(x) for x in list(self.task_managers.keys())]) + 1
        txt = ""
        for ch, worker in list(self.task_managers.items()):
            sname = TaskManager.STATE_NAMES[worker.task_manager.get_state()]
            txt += "channel: {:<{width}}, id = {:<{width}}, state = {:<10} \n".format(ch,
                                                                                      worker.task_manager.id,
                                                                                      sname,
                                                                                      width=width)
            channel_config = self.config_manager.get_channels()[ch]
            for i in ("sources",
                      "transforms",
                      "logicengines",
                      "publishers"):
                txt += "\t{}:\n".format(i)
                modules = channel_config.get(i, {})
                for mod_name, mod_config in modules.items():
                    txt += "\t\t{}\n".format(mod_name)
                    my_module = importlib.import_module(
                        mod_config.get('module'))
                    produces = None
                    consumes = None
                    try:
                        produces = getattr(my_module, 'PRODUCES')
                    except AttributeError:
                        pass
                    try:
                        consumes = getattr(my_module, 'CONSUMES')
                    except AttributeError:
                        pass
                    txt += "\t\t\tconsumes : {}\n".format(consumes)
                    txt += "\t\t\tproduces : {}\n".format(produces)
        txt += self.reaper_status()
        return txt[:-1]

    def rpc_stop(self):
        self.reaper_stop()
        self.stop_channels()
        self.shutdown()
        return "OK"

    def rpc_start_channel(self, channel):
        self.reload_config()
        if channel in self.task_managers:
            return "ERROR, channel {} is running".format(channel)
        self.start_channel(channel)
        return "OK"

    def start_channel(self, channel):
        channel_config = self.config_manager.get_channels()[channel]
        generation_id = 1
        taskmanager_id = str(uuid.uuid4()).upper()
        task_manager = TaskManager.TaskManager(channel,
                                               taskmanager_id,
                                               generation_id,
                                               channel_config,
                                               self.config_manager.get_global_config())
        worker = Worker(task_manager,
                        self.config_manager.get_global_config())
        self.task_managers[channel] = worker
        worker.start()
        self.logger.info("Channel {} started".format(channel))

    def rpc_start_channels(self):
        self.reload_config()
        self.start_channels()
        return "OK"

    def start_channels(self):
        channels = self.config_manager.get_channels()
        if not channels:
            raise RuntimeError("No channels configured")
        """
        start channels
        """
        for ch in channels:
            try:
                self.start_channel(ch)
            except Exception as e:
                self.logger.error(
                    "Channel {} failed to start : {}".format(ch, e))

    def rpc_stop_channel(self, channel):
        self.stop_channel(channel)
        return "OK"

    def stop_channel(self, channel):
        worker = self.task_managers[channel]
        if worker.task_manager.get_state() not in (TaskManager.SHUTTINGDOWN,
                                                   TaskManager.SHUTDOWN):
            worker.task_manager.set_state(TaskManager.SHUTTINGDOWN)
        for i in range(int(self.config_manager.config.get("shutdown_timeout", 10))):
            if worker.task_manager.get_state() == TaskManager.SHUTDOWN:
                break
            else:
                time.sleep(1)
                continue
        worker.terminate()
        del self.task_managers[channel]

    def rpc_stop_channels(self):
        self.stop_channels()
        return "OK"

    def stop_channels(self):
        for x in self.task_managers.items():
            x[1].task_manager.set_state(TaskManager.SHUTTINGDOWN)
        channels = list(self.task_managers.keys())
        for ch in channels:
            self.stop_channel(ch)

    def handle_sighup(self, signum, frame):
        self.reaper_stop()
        self.stop_channels()
        self.reload_config()
        self.start_channels()
        self.reaper_start(delay=self.global_config.get('dataspace', ('reaper_start_delay_seconds', 1818)))

    def rpc_reload_config(self):
        self.reload_config()
        return "OK"

    def reload_config(self):
        self.config_manager.reload()

    def rpc_get_log_level(self):
        engineloglevel = self.get_logger().getEffectiveLevel()
        txt = "{}".format(LOG_LEVELS_DICT[engineloglevel])
        return txt

    def rpc_get_channel_log_level(self, channel):
        worker = self.task_managers[channel]
        loglevel = LOG_LEVELS_DICT[worker.task_manager.get_loglevel()]
        txt = "{} ".format(loglevel)
        return txt[:-1]

    def rpc_set_channel_log_level(self, channel, log_level):
        worker = self.task_managers[channel]
        if worker.task_manager.get_loglevel() == TaskManager.LOG_LEVELS_DICT[log_level]:
            txt = "Nothing to do. Current log level is : {} ".format(log_level)
            return txt[:-1]
        else:
            worker.task_manager.set_loglevel(TaskManager.LOG_LEVELS_DICT[log_level])
            txt = "Log level changed to : {} ".format(log_level)
            return txt[:-1]

    def rpc_reaper_start(self, delay=0):
        '''
        Start the reaper process after 'delay' seconds.
        Default 0 seconds delay.
        :type delay: int
        '''
        self.reaper_start(delay)
        return "OK"

    def reaper_start(self, delay):
        self.reaper.start(delay)

    def rpc_reaper_stop(self):
        self.reaper_stop()
        return "OK"

    def reaper_stop(self):
        self.reaper.stop()

    def rpc_reaper_status(self):
        interval = self.reaper.get_retention_interval()
        state = self.reaper.get_state()
        txt = 'reaper:\n\tstate: {}\n\tretention_interval: {}'.format(state, interval)
        return txt

    def reaper_status(self):
        interval = self.reaper.get_retention_interval()
        state = self.reaper.get_state()
        txt = '\nreaper:\n\tstate: {}\n\tretention_interval: {}\n'.format(state, interval)
        return txt

def parse_program_options(args):
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", default=8888, type=int, choices=range(1, 65535), help="Override server port to this value")
    options = parser.parse_args(args)
    return {
        'server_address': ['localhost', options.port] # Use Jsonnet-supported schema (i.e. not a tuple)
    }

def main(args=None):
    '''If you pass a list of args, they will be used instead of sys.argv'''

    program_options = parse_program_options(args)
    conf_manager = Conf_Manager.ConfigManager(program_options)

    try:
        conf_manager.load()
    except Exception as msg:
        sys.exit("Failed to load configuration {}\n{}".format(conf_manager.config_dir, msg))

    channels = conf_manager.get_channels()
    channels_required = not os.getenv('DECISIONENGINE_NO_CHANNELS')

    if channels_required and not channels:
        sys.exit("No channel configurations available in {}".format(conf_manager.config_dir))

    global_config = conf_manager.get_global_config()
    server_address = tuple(global_config.get('server_address'))

    try:
        server = DecisionEngine(conf_manager, server_address, RequestHandler)
        server.reaper_start(delay=global_config['dataspace'].get('reaper_start_delay_seconds', 1818))
        if channels_required:
            server.start_channels()
        server.serve_forever()
    except Exception as msg:
        sys.exit("Server Address: {}\n".format(server_address) +
                 "Config Dir: {}\n".format(conf_manager.config_dir) +
                 "Fatal Error: {}\n".format(msg))


if __name__ == "__main__":
    main()
