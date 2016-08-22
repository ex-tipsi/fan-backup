from unittest import TestCase

from basictracer import BasicTracer
from basictracer.recorder import InMemoryRecorder
from fan.context import TracedContext
from fan.discovery import (SimpleDictDiscovery,
                           LocalDiscovery, CompositeDiscovery)
from fan.service import ServiceGroup, Service, endpoint
from fan.process import Process
from fan.remote import LocalEndpoint, RemoteEndpoint, Transport


def get_simple_discovery():
    conf = {'connections': [{'connection': 'main_redis',
                             'transport': 'redis',
                             'params': {'host': 'localhost',
                                        'port': '6789',
                                        'db': '0'}},
                            {'connection': 'main',
                             'transport': 'stringio',
                             'params': {'bucket': 'default'}}]}
    return CompositeDiscovery(LocalDiscovery(), SimpleDictDiscovery(conf))


class DummyTracer(Service):
    service_name = 'dummy_tracer'

    @endpoint('echo')
    def echo(self, ctx, word, count):
        if count > 0:
            return ctx.rpc.dummy_tracer.echo(word, count-1)
        else:
            return word, 0


class TestServiceGroup(ServiceGroup):
    def __init__(self, discovery):
        super().__init__(discovery)
        self.endpoints = []

    def start(self):
        for data in self.services:
            service = data['service']()
            service.on_start()

            self.instances.append(service)
            for ep_conf in data['endpoints']:
                ep = RemoteEndpoint(self.discovery, service, ep_conf['params'])
                ep.on_start()
                self.endpoints.append(ep)
                self.discovery.register(ep)


class DummyTransport(Transport):
    storage = {}

    def __init__(self, discovery, endpoint, params):
        super().__init__(discovery, endpoint, params)
        if isinstance(endpoint, RemoteEndpoint):
            self.storage[params['id']] = self

    def rpc_call(self, method, ctx, *args, **kwargs):
        remote_ep = self.storage[self.params['id']]
        return remote_ep.handle_call(method, ctx, *args, **kwargs)


class DummyServiceGroup(TestServiceGroup):
    services = [{'service': DummyTracer,
                 'endpoints': [{'endpoint': RemoteEndpoint,
                                'params': {'transport': DummyTransport, 'id': 1}}]}]


class ChainedEchoService(Service):
    service_name = 'chained_echo'

    @endpoint('echo')
    def echo(self, ctx, word):
        return ctx.rpc.dummy_tracer.echo(word, 0)[0]


class ChainedServiceGroup(TestServiceGroup):
    services = [{'service': ChainedEchoService,
                 'endpoints': [{'endpoint': RemoteEndpoint,
                                'params': {'transport': DummyTransport, 'id': 1}}]}]


class EchoService(Service):
    service_name = 'simple_echo'

    @endpoint('echo')
    def echo(self, ctx, word):
        return word


class TestProcess(Process):
    def create_context(self):
        return TracedContext(self.discovery)


class ProcessTestCase(TestCase):
    def setUp(self):
        self.recorder = InMemoryRecorder()
        d = get_simple_discovery()
        d.tracer = BasicTracer(self.recorder)

        self.process = TestProcess(d)

    def test_call(self):
        context = self.process.create_context()

        context.discovery.register(LocalEndpoint(DummyTracer()))
        response = context.rpc.dummy_tracer.echo('hello', 7)

        self.assertEquals(len(self.recorder.get_spans()), 8)
        assert response == ('hello', 0), response


class TestRemoteDiscovery(SimpleDictDiscovery):
    def __init__(self, conf):
        super().__init__(conf)
        self.conf = conf
        self.remote = None

    def link(self, other_remote):
        if not self.remote:
            self.remote = other_remote
            self.remote.link(self)

    def find_local_endpoint(self, service_name):
        print('Remote Lookup: {} {}'.format(service_name, self.cached_endpoints))
        if service_name in self.cached_endpoints:
            return self.cached_endpoints[service_name]
        assert False, 'Cannot find: {} {}'.format(service_name, self.cached_endpoints)

    def find_remote_endpoint(self, service_name):
        pass


class MultiProcessTestCase(TestCase):
    def setUp(self):
        self.recorder = InMemoryRecorder()

        self.remote = TestRemoteDiscovery({})

        self.p1 = self.create_process(self.recorder, ChainedServiceGroup)
        self.p2 = self.create_process(self.recorder, DummyServiceGroup)
        self.p1.start()
        self.p2.start()

    def create_process(self, recorder, sg):
        discovery = CompositeDiscovery(LocalDiscovery(), self.remote)
        discovery.tracer = BasicTracer(recorder)
        proc = TestProcess(discovery)
        proc.service_groups = [sg]
        return proc

    def test_call(self):
        context = self.p1.create_context()
        result = context.rpc.chained_echo.echo('hello')

        self.assertEqual(result, 'hello')