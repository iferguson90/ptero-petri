import unittest
try:
    from unittest import mock
except:
    import mock

from ptero_petri.implementation.brokers.amqp.channel_facade import ChannelFacade
from twisted.internet import defer

class ChannelFacadeTests(unittest.TestCase):
    def setUp(self):
        self.cm = mock.Mock()
        self.cm.connect = mock.Mock()
        self.connect_deferred = defer.Deferred()
        self.cm.connect.return_value = self.connect_deferred
        self.cf = ChannelFacade(connection_manager=self.cm)

    def test_init(self):
        self.assertIs(self.cf._pika_channel, None)
        self.assertIs(self.cf._publisher_confirm_manager, None)
        self.assertEqual(self.cf._last_publish_tag, 0)

    def test_private_on_connected(self):
        fake_pcm = mock.Mock()
        fake_ready_deferred = mock.Mock()
        with mock.patch(
                'ptero_petri.implementation.brokers.amqp.channel_facade.PublisherConfirmManager',
                new=fake_pcm):
            fake_pika_channel = mock.Mock()
            self.cf._on_connected(fake_pika_channel,
                    ready_deferred=fake_ready_deferred)

            fake_pcm.assert_called_once_with(fake_pika_channel)
            self.assertIs(self.cf._pika_channel, fake_pika_channel)

    def test_basic_publish(self):
        # todo
        pass


    def test_basic_ack(self):
        self.cf._pika_channel = mock.Mock()
        self.cf._pika_channel.basic_ack = mock.Mock()
        expected_return_value = mock.Mock()
        self.cf._pika_channel.basic_ack.return_value = expected_return_value
        recieve_tag = mock.Mock()
        return_value = self.cf.basic_ack(recieve_tag)

        self.assertIs(return_value, expected_return_value)
        self.cf._pika_channel.basic_ack.assert_called_once_with(recieve_tag)

    def test_basic_reject(self):
        self.cf._pika_channel = mock.Mock()
        self.cf._pika_channel.basic_reject = mock.Mock()
        expected_return_value = mock.Mock()
        self.cf._pika_channel.basic_reject.return_value = expected_return_value
        recieve_tag = mock.Mock()
        requeue = mock.Mock()
        return_value = self.cf.basic_reject(recieve_tag=recieve_tag,
                requeue=requeue)

        self.assertIs(return_value, expected_return_value)
        self.cf._pika_channel.basic_reject.assert_called_once_with(recieve_tag,
                requeue=requeue)

    def test_private_connect_and_do(self):
        fn_name = mock.Mock()
        arg1 = mock.Mock()
        arg2 = mock.Mock()
        kwarg1 = mock.Mock()
        kwarg2 = mock.Mock()

        # for _pika_channel = None
        fake_deferred = mock.Mock()
        FakeDeferred = mock.Mock(return_value=fake_deferred)
        with mock.patch('twisted.internet.defer.Deferred', new=FakeDeferred):
            self.cf._pika_channel = None

            connect_deferred = mock.Mock()
            connect_deferred.addCallback = mock.Mock()
            self.cf.connect = mock.Mock(return_value=connect_deferred)

            return_value = self.cf._connect_and_do(fn_name, arg1, arg2,
                    kwarg1=kwarg1, kwarg2=kwarg2)
            self.assertIs(return_value, fake_deferred)
            self.assertEqual(self.cf.connect.call_count, 1)
            connect_deferred.addCallback.assert_called_once_with(
                    self.cf._do_on_channel,
                    fn_name=fn_name, args=(arg1, arg2),
                    kwargs={'kwarg1':kwarg1, 'kwarg2':kwarg2},
                    deferred=fake_deferred)

        # for _pika_channel != None
        fn_name = 'fake_fn_name'
        fake_pika_channel = mock.Mock()
        expected_return_value = mock.Mock()
        fake_pika_channel.fake_fn_name = mock.Mock(
                return_value=expected_return_value)

        self.cf._pika_channel = fake_pika_channel
        return_value = self.cf._connect_and_do(fn_name, arg1, arg2,
                kwarg1=kwarg1, kwarg2=kwarg2)
        self.assertIs(return_value, expected_return_value)
        fake_pika_channel.fake_fn_name.assert_called_once_with(arg1, arg2,
                kwarg1=kwarg1, kwarg2=kwarg2)

    def test_static_do_on_channel(self):
        arg1 = mock.Mock()
        arg2 = mock.Mock()
        kwarg1 = mock.Mock()
        kwarg2 = mock.Mock()

        fn_name = 'fake_fn_name'
        fake_pika_channel = mock.Mock()
        this_things_deferred = mock.Mock()
        fake_pika_channel.fake_fn_name = mock.Mock(
                return_value=this_things_deferred)

        deferred = mock.Mock()
        return_value = self.cf._do_on_channel(fake_pika_channel, fn_name,
                args=(arg1, arg2),
                kwargs={'kwarg1':kwarg1, 'kwarg2':kwarg2},
                deferred=deferred)
        self.assertIs(return_value, fake_pika_channel)

        fake_pika_channel.fake_fn_name.assert_called_once_with(arg1, arg2,
                kwarg1=kwarg1, kwarg2=kwarg2)
        this_things_deferred.chainDeferred.assert_called_once_with(deferred)

