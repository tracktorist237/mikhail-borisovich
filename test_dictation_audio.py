import unittest
from unittest.mock import patch
from array import array
from dictation_audio import input_routes, levels, temporary_mic_boost


class DictationAudioTests(unittest.TestCase):
    def test_routes_match_firefox_pid_and_flag_sink_monitor(self):
        def node(id, **props):
            return {'id':id,'type':'PipeWire:Interface:Node','info':{'props':props}}
        nodes = [node(1, **{'media.class':'Stream/Input/Audio','application.process.id':42}),
                 node(2, **{'media.class':'Stream/Input/Audio','application.process.id':99}),
                 node(3, **{'media.class':'Audio/Source','node.name':'physical','object.serial':300}),
                 node(4, **{'media.class':'Audio/Sink','node.name':'speaker','object.serial':400}),
                 {'id':5,'type':'PipeWire:Interface:Port','info':{'props':{'port.physical':True,'port.name':'capture_FL'}}},
                 {'id':6,'type':'PipeWire:Interface:Port','info':{'props':{'port.monitor':True,'port.name':'monitor_FL'}}}]
        for id, target, source, port in [(10,1,3,5),(11,1,4,6),(12,2,3,5)]:
            nodes.append({'id':id,'type':'PipeWire:Interface:Link','info':{
                'input-node-id':target,'output-node-id':source,'output-port-id':port,'state':'active'}})
        routes = input_routes(nodes, 42)
        self.assertEqual(len(routes), 2)
        self.assertTrue(routes[0]['physical'])
        self.assertFalse(routes[0]['monitor'])
        self.assertTrue(routes[1]['monitor'])
        self.assertEqual(routes[0]['serial'], 300)
        self.assertEqual(input_routes(nodes, 100), [])

    def test_boost_restored_after_failure(self):
        with patch('dictation_audio.subprocess.check_output',return_value='  : values=3,2'), patch(
                'dictation_audio.subprocess.run') as run:
            with self.assertRaises(RuntimeError):
                with temporary_mic_boost(0):
                    raise RuntimeError('probe failed')
            self.assertEqual(run.call_args_list[0].args[0][-1], '0')
            self.assertEqual(run.call_args_list[-1].args[0][-1], '3,2')

    def test_channels_do_not_cancel_each_other(self):
        self.assertEqual(levels(array('h',[400,-400]*800).tobytes()),
                         [(400.0,400),(400.0,400)])
        self.assertEqual(levels(array('h',[0,0]*800).tobytes()),[(0.0,0),(0.0,0)])


if __name__ == '__main__':unittest.main()
