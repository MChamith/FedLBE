import ast
import json
import os
import uuid
import websockets
import asyncio
import torch
from torch.utils.data import DataLoader
import copy
import numpy as np
from tqdm import tqdm
import importlib
import inspect
from pathlib import Path
import sys
from modelUtil import dequantize_tensor, decompress_tensor
import pickle
from DataLoaders.loaderUtil import getDataloader
from providers import llm_provider
from utils import create_message, create_message_results, create_result_dict, create_message_model_check
from modelUtil import get_criterion
from DBService import db_service
from Scheduler import Scheduler
import time


class LLMProcessor:

    def __init__(self):
        self.new_latencies = None
        self.num_clients = 0
        self.local_weights = []
        self.local_loss = []
        self.q_diff, self.info = [], []
        self.v, self.i, self.s = [], [], []
        self.bytes = []
        self.comp_len = 0
        self.model_success = False
        self.data_config = None

    def load_dataset(self, folder):

        data_test = np.load('data/' + str(folder) + '/X.npy')
        labels = np.load('data/' + str(folder) + '/y.npy')
        return data_test, labels

    def testing(self, model, preprocessing, bs, criterion):

        dataset, labels = self.load_dataset(preprocessing['folder'])
        test_loss = 0
        correct = 0
        test_loader = DataLoader(getDataloader(dataset, labels, preprocessing), batch_size=bs, shuffle=False)
        model.eval()
        for data, label in test_loader:
            output = model(data)
            loss = criterion(output, label)
            test_loss += loss.item() * data.size(0)
            if preprocessing['dtype'] != 'One D':
                _, pred = torch.max(output, 1)
                correct += pred.eq(label.data.view_as(pred)).sum().item()

        test_loss /= len(test_loader.dataset)
        if preprocessing['dtype'] != 'One D':
            test_accuracy = 100. * correct / len(test_loader.dataset)
        else:
            test_accuracy = 0

        return test_loss, test_accuracy

    async def connector(self, client_uri, data, client_index, server_socket):
        """connector function for connecting the server to the clients. This function is called asynchronously to
        1. send process requests to each client
        2. calculate local weights for each client separately"""

        async with websockets.connect(client_uri, ping_interval=None, max_size=3000000) as websocket:
            finished = False
            try:
                await websocket.send(data)
                start = time.time()
                while not finished:
                    async for message in websocket:
                        try:

                            data = pickle.loads(message)
                            self.bytes.append(len(message))
                            if len(data) == 2:
                                self.local_weights.append(copy.deepcopy(data[0]))
                                self.local_loss.append(copy.deepcopy(data[1]))

                            elif len(data) == 3:
                                self.q_diff.append(copy.deepcopy(data[0]))
                                self.local_loss.append(copy.deepcopy(data[1]))
                                self.info.append(copy.deepcopy(data[2]))
                                self.comp_len = len(self.q_diff)

                            elif len(data) == 4:
                                self.v.append(copy.deepcopy(data[0]))
                                self.i.append(copy.deepcopy(data[1]))
                                self.s.append(copy.deepcopy(data[2]))
                                self.local_loss.append(copy.deepcopy(data[3]))
                                self.comp_len = len(self.v)

                            finished = True
                            self.new_latencies[0, client_index] = time.time() - start
                            break

                        except Exception as e:
                            print('exceptiop ' + str(e))
                            if type(message) is str:
                                print('message ' + str(message))
                                msg = ast.literal_eval(message)

                                if 'type' in msg:
                                    if msg['type'] == 'model':
                                        if msg['status'] == 'success':
                                            print('model successful')
                                            self.model_success = True
                                    elif msg['type'] == 'data':
                                        self.data_config = message

                                    finished = True
                                else:
                                    await server_socket.send(message)

                print('closed')
            except Exception as e:
                print('exception ' + str(e))

    async def start_process(self, data, websocket):

        global model, model_arch
        print('start job called')
        job_id = uuid.uuid4().hex
        # job_id = '317343971b814ab99c4f1ddbb04c9897'
        print('data ' + str(data))
        print('job id ' + str(job_id))

        job_data = data['jobData']
        # dataset = job_data['scheme']['dataset']
        schemeData = job_data['scheme']
        client_list = job_data['general']['clients']
        T = int(schemeData['comRounds'])
        C = float(schemeData['clientFraction']) if 'clientFraction' in schemeData else 1
        schemeData['clientFraction'] = C
        K = int(len(client_list))
        E = int(schemeData['epoch'])
        eta = float(schemeData['lr'])
        B = int(schemeData['minibatch'])
        B_test = int(schemeData['minibatchtest'])
        dataset = {'folder': schemeData['dataset'], 'dtype': schemeData['dtype']}
        model_param = {'optimizer': str(schemeData['optimizer']), 'loss': schemeData['loss'], }

        if schemeData['compress'] != 'No':
            model_param['compress'] = schemeData['compress']
            model_param['z_point'] = 0.1
            model_param['scale'] = 0
            model_param['num_bits'] = schemeData['compress_bits']

        else:
            model_param['compress'] = False

        scheduler_type = schemeData['scheduler']

        latency_avg = 4 if scheduler_type == 'latency_proportional' else 1

        criterion = get_criterion(schemeData['loss'])

        model_check_uri = 'ws://' + str(client_list[0]['client_ip']) + '/get_data_config'

        await self.connector(model_check_uri, pickle.dumps([dataset['folder']]), 0, websocket)

        for i in range(10):

            model_arch = llm_provider.get_model_architecture(self.data_config)
            model_check_uri_ip = str(client_list[0]['client_ip'])
            model_check_data = create_message_model_check(B, dataset, model_arch, model_param)
            model_check_uri = 'ws://' + model_check_uri_ip + '/check_model'

            await self.connector(model_check_uri, model_check_data, 0, websocket)
            print('testing ' + str(i) + 'th model')
            if self.model_success:
                print('found a successful model architecture')
                break

        print('proceeding after successful model')
        filename = "./ModelData/" + str(job_id) + '/Model.py'

        os.makedirs(os.path.dirname(filename), exist_ok=True)

        with open(filename, 'w') as f:
            f.write(model_arch)
        print('file written')
        path_pyfile = Path(filename)
        sys.path.append(str(path_pyfile.parent))
        mod_path = str(path_pyfile).replace('\\', '.').strip('.py')
        imp_path = importlib.import_module(mod_path)

        for name_local in dir(imp_path):

            if inspect.isclass(getattr(imp_path, name_local)):
                print(f'{name_local} is a class')
                modelClass = getattr(imp_path, name_local)
                model = modelClass()

        model_file = path_pyfile.read_bytes()

        global_weights = model.state_dict()
        train_loss = []
        test_loss = []
        test_accuracy = []
        round_times = []
        total_bytes = []
        # m = max(int(C * K), 1)

        # run for number of communication rounds
        scheduler = Scheduler(scheduler_type, K, C, avg_rounds=latency_avg)
        for curr_round in tqdm(range(1, T + 1)):
            start_time = time.time()
            # TODO need to check

            # S_t = np.random.choice(range(K), m, replace=False)
            S_t = scheduler.get_workers(self.new_latencies)
            self.new_latencies = np.ones((1, K), dtype='float')
            client_ports = [clt for clt in client_list]
            clients = [client_ports[i] for i in S_t]
            st_count = 0

            print('clients ' + str(clients))
            tasks = []
            for client in clients:
                client_uri = 'ws://' + str(client['client_ip']) + '/process'
                print(client_uri)
                serialized_data = create_message(B, eta, E, model_file, model_param,
                                                 dataset, global_weights)
                client_index = client_ports.index(client)
                tasks.append(self.connector(client_uri, serialized_data, client_index, websocket))
                st_count += 0
            await asyncio.gather(*tasks)
            print('latencies ' + str(self.new_latencies))

            if model_param['compress']:
                print('compressing')

                for i in range(self.comp_len):

                    count = 0
                    for server_param in model.parameters():
                        if model_param['compress'] == 'quantize':

                            z_point = float(model_param['z_point'])
                            scale = float(model_param['scale'])
                            server_param.data += dequantize_tensor(self.q_diff[i][count], scale, z_point,
                                                                   self.info[i][count]) / len(self.q_diff)
                        else:

                            server_param.data += decompress_tensor(self.v[i][count], self.i[i][count],
                                                                   self.s[i][count]) / len(self.v)
                        count += 1

                global_weights = model.state_dict()

            else:
                print('not compressing')
                # TODO local weights are not reset check it
                weights_avg = copy.deepcopy(self.local_weights[0])
                for k in weights_avg.keys():
                    for i in range(1, len(self.local_weights)):
                        weights_avg[k] += self.local_weights[i][k]

                    weights_avg[k] = torch.div(weights_avg[k], len(self.local_weights))

                global_weights = weights_avg
            # torch.save(model.state_dict(), "./ModelData/" + str(job_id) + '/model.pt')
            torch.save(model.state_dict(), 'model.pt')
            model.load_state_dict(global_weights)

            loss_avg = sum(self.local_loss) / len(self.local_loss)
            train_loss.append(loss_avg)
            print('train loss ' + str(loss_avg))
            g_loss, g_accuracy = self.testing(model, dataset, B_test, criterion)

            print('test accuracy ' + str(g_accuracy))
            test_loss.append(g_loss)
            test_accuracy.append(g_accuracy)
            elapsed_time = round(time.time() - start_time, 2)
            if len(round_times) > 0:
                tot_time = round_times[-1] + elapsed_time
            else:
                tot_time = elapsed_time

            round_times.append(tot_time)
            if len(total_bytes) > 0:
                tot_bytes = total_bytes[-1] + self.bytes[-1] / 1e6
            else:
                tot_bytes = self.bytes[-1] / 1e6
            total_bytes.append(round(tot_bytes, 2))

            serialized_results = create_message_results(test_accuracy, train_loss, test_loss, curr_round, round_times,
                                                        total_bytes)
            result_dict = create_result_dict(test_accuracy, train_loss, test_loss, curr_round, elapsed_time)
            # db_service.save_results(result_dict, job_id)
            await websocket.send(serialized_results)

            print('calculated results for round ' + str(curr_round))