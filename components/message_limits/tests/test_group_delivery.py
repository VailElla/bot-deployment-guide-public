import asyncio
from collections import defaultdict
import contextvars
import importlib.util
import json
import logging
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

spec=importlib.util.spec_from_file_location('group_delivery_under_test',Path(__file__).resolve().parents[1]/'astrbot_plugin_message_limits/group_delivery.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)

class Tests(unittest.IsolatedAsyncioTestCase):
 async def asyncSetUp(self):
  self.temp=tempfile.TemporaryDirectory();self.settings=Path(self.temp.name)/'settings.json';self.settings.write_text('{"enabled":true}')
  self.controller=m.GroupDelivery(self.settings,logging.getLogger('test.delivery'))
  self.ev=contextvars.ContextVar('current',default=None);self.registry=SimpleNamespace(_agent_stop_callbacks={})
  self.registry.register_agent_stop_callback=lambda e,c:self.registry._agent_stop_callbacks.__setitem__(e,c)
  self.registry.unregister_agent_stop_callback=lambda e:self.registry._agent_stop_callbacks.pop(e,None)
  self.history=defaultdict(list);self.locks=defaultdict(asyncio.Lock);self.model_started={};self.tts_started={};self.delivered=[];outer=self
  class Event:
   def __init__(self,name,group='1',platform='snowluma'):
    self.name=name;self.group=group;self.platform=platform;self.extra={};self.result=None;self.stopped=False
    self.platform_meta=SimpleNamespace(support_streaming_message=False);self.req=None
   def get_group_id(self):return self.group
   def get_self_id(self):return 'bot'
   def get_platform_id(self):return self.platform
   def get_extra(self,k):return self.extra.get(k)
   def set_extra(self,k,v):self.extra[k]=v
   def is_stopped(self):return self.stopped
   def set_result(self,v):self.result=v
   def get_result(self):return self.result
   def clear_result(self):self.result=None
   @classmethod
   async def send_message(cls,bot,message_chain,event=None,is_group=False,session_id=None):
    owner=outer.ev.get()
    if owner.extra.get('send_fail'):raise OSError('synthetic send failure')
    outer.delivered.append((owner.name,list(message_chain.chain)))
  class Stage:
   streaming_response=False;unsupported_streaming_strategy='turn_off'
   async def _save_to_history(self,e,*a,**kw):
    if e.extra.get('save_fail'):raise OSError('synthetic history failure')
    outer.history[e.group].append(e.name)
   async def process(self,e,*args):
    async with outer.locks[e.group]:
     e.req=SimpleNamespace(conversation=SimpleNamespace(cid='cid'),system_prompt='persona')
     if e.extra.get('no_conversation'):e.req.conversation=None
     await outer.controller.prepare(e,e.req)
     e.seen_history=list(outer.history[e.group]);outer.model_started.setdefault(e.name,asyncio.Event()).set()
     if e.extra.get('model_gate'):await e.extra['model_gate'].wait()
     if e.extra.get('tool_send'):
      await Event.send_message(None,SimpleNamespace(chain=['tool']),is_group=True,session_id=e.group)
     result=SimpleNamespace(chain=[e.name],async_stream=None)
     e.set_result(result);yield None
     if not e.extra.get('leave_result'):e.clear_result()
     await self._save_to_history(e)
  self.Event,self.Stage=Event,Stage
  self.original_process=Stage.process;self.original_send=Event.__dict__['send_message']
  self.controller.install(Stage,Event,self.registry,self.ev)
 async def asyncTearDown(self):
  await self.controller.close();self.temp.cleanup()
 async def run_event(self,e):
  token=self.ev.set(e);stream=self.Stage().process(e)
  try:
   async for _ in stream:
    self.tts_started.setdefault(e.name,asyncio.Event()).set()
    if e.extra.get('tts_gate'):await e.extra['tts_gate'].wait()
    await self.Event.send_message(None,e.get_result(),is_group=True,session_id=e.group)
  finally:
   await stream.aclose();self.ev.reset(token)
 async def wait_model(self,name):
  await asyncio.wait_for(self.model_started.setdefault(name,asyncio.Event()).wait(),1)
 async def wait_tts(self,name):
  await asyncio.wait_for(self.tts_started.setdefault(name,asyncio.Event()).wait(),1)
 async def test_next_model_overlaps_tts_and_reads_committed_history(self):
  gate=asyncio.Event();a=self.Event('A');a.extra['tts_gate']=gate;b=self.Event('B')
  ta=asyncio.create_task(self.run_event(a));await self.wait_tts('A')
  tb=asyncio.create_task(self.run_event(b));await self.wait_model('B');await asyncio.sleep(.01)
  self.assertEqual(b.seen_history,['A']);self.assertIn('1条',b.req.system_prompt)
  self.assertFalse(self.tts_started.get('B',asyncio.Event()).is_set());self.assertEqual(self.delivered,[])
  gate.set();await asyncio.gather(ta,tb)
  self.assertEqual([n for n,_ in self.delivered],['A','B'])
 async def test_model_remains_serial(self):
  gate=asyncio.Event();a=self.Event('A');a.extra['model_gate']=gate;b=self.Event('B')
  ta=asyncio.create_task(self.run_event(a));await self.wait_model('A');tb=asyncio.create_task(self.run_event(b));await asyncio.sleep(.01)
  self.assertFalse(self.model_started.get('B',asyncio.Event()).is_set());gate.set();await asyncio.gather(ta,tb)
 async def test_other_groups_do_not_wait_delivery(self):
  gate=asyncio.Event();a=self.Event('A');a.extra['tts_gate']=gate;b=self.Event('B','2')
  ta=asyncio.create_task(self.run_event(a));await self.wait_tts('A');await self.run_event(b)
  self.assertEqual([n for n,_ in self.delivered],['B']);gate.set();await ta
 async def test_failed_send_releases_queue_and_marks_next_context(self):
  a=self.Event('A');a.extra['send_fail']=True
  with self.assertRaises(OSError):await self.run_event(a)
  b=self.Event('B');await self.run_event(b)
  self.assertIn('未确认全部发送成功',b.req.system_prompt);self.assertEqual([n for n,_ in self.delivered],['B'])
 async def test_cancel_delivery_releases_following_reply(self):
  gate=asyncio.Event();a=self.Event('A');a.extra['tts_gate']=gate;b=self.Event('B')
  ta=asyncio.create_task(self.run_event(a));await self.wait_tts('A');tb=asyncio.create_task(self.run_event(b));await self.wait_model('B')
  a.extra['agent_stop_requested']=True;self.registry._agent_stop_callbacks[a]()
  with self.assertRaises(asyncio.CancelledError):await ta
  await tb;self.assertEqual([n for n,_ in self.delivered],['B'])
 async def test_cancel_waiter_does_not_cancel_predecessor_future(self):
  gate=asyncio.Event();a=self.Event('A');a.extra['tts_gate']=gate;b=self.Event('B')
  ta=asyncio.create_task(self.run_event(a));await self.wait_tts('A');tb=asyncio.create_task(self.run_event(b));await self.wait_model('B');await asyncio.sleep(.01)
  tb.cancel()
  with self.assertRaises(asyncio.CancelledError):await tb
  self.assertFalse(a.extra[m.TICKET]['done'].cancelled());gate.set();await ta
 async def test_tool_direct_send_cannot_overtake(self):
  gate=asyncio.Event();a=self.Event('A');a.extra['tts_gate']=gate;b=self.Event('B');b.extra['tool_send']=True
  ta=asyncio.create_task(self.run_event(a));await self.wait_tts('A');tb=asyncio.create_task(self.run_event(b));await self.wait_model('B');await asyncio.sleep(.01)
  self.assertEqual(self.delivered,[]);gate.set();await asyncio.gather(ta,tb)
  self.assertEqual([n for n,_ in self.delivered],['A','B','B'])
 async def test_history_failure_does_not_deliver_buffered_answer(self):
  a=self.Event('A');a.extra['save_fail']=True
  with self.assertRaises(OSError):await self.run_event(a)
  self.assertEqual(self.delivered,[])
 async def test_leftover_result_not_duplicated(self):
  a=self.Event('A');a.extra['leave_result']=True;await self.run_event(a)
  self.assertEqual(len(self.delivered),1)
 async def test_private_chat_remains_original_order(self):
  a=self.Event('A',group='');await self.run_event(a)
  self.assertNotIn(m.TICKET,a.extra)
 async def test_streaming_excluded(self):
  self.Stage.streaming_response=True;self.Stage.unsupported_streaming_strategy='realtime_segmenting'
  a=self.Event('A');await self.run_event(a);self.assertNotIn(m.TICKET,a.extra)
 async def test_no_conversation_passes_through(self):
  a=self.Event('A');a.extra['no_conversation']=True;await self.run_event(a)
  self.assertFalse(a.extra[m.ACTIVE]);self.assertEqual(len(self.delivered),1)
 async def test_disabled_waits_for_already_pending_delivery(self):
  gate=asyncio.Event();a=self.Event('A');a.extra['tts_gate']=gate
  ta=asyncio.create_task(self.run_event(a));await self.wait_tts('A');self.settings.write_text('{"enabled":false}')
  b=self.Event('B');tb=asyncio.create_task(self.run_event(b));await asyncio.sleep(.01);self.assertFalse(self.model_started.get('B',asyncio.Event()).is_set())
  gate.set();await asyncio.gather(ta,tb);self.assertEqual([n for n,_ in self.delivered],['A','B'])
 async def test_backpressure_bounds_pending_delivery(self):
  gate=asyncio.Event();events=[self.Event(str(i)) for i in range(9)];events[0].extra['tts_gate']=gate
  tasks=[asyncio.create_task(self.run_event(events[0]))];await self.wait_tts('0')
  tasks += [asyncio.create_task(self.run_event(e)) for e in events[1:]]
  await self.wait_model('7');await asyncio.sleep(.01)
  self.assertFalse(self.model_started.get('8',asyncio.Event()).is_set())
  self.assertEqual(len(self.controller.group(events[0])['tickets']),8)
  gate.set();await asyncio.gather(*tasks)
  self.assertEqual([n for n,_ in self.delivered],[str(i) for i in range(9)])
 async def test_stop_before_send_drops_reply(self):
  a=self.Event('A');token=self.ev.set(a);stream=self.Stage().process(a)
  try:
   await anext(stream);a.extra['agent_stop_requested']=True
   await self.Event.send_message(None,a.get_result(),is_group=True,session_id=a.group)
   self.assertEqual(self.delivered,[])
  finally:await stream.aclose();self.ev.reset(token)
 async def test_cancelled_close_keeps_draining(self):
  gate=asyncio.Event();a=self.Event('A');a.extra['tts_gate']=gate
  ta=asyncio.create_task(self.run_event(a));await self.wait_tts('A')
  closing=asyncio.create_task(self.controller.close());await asyncio.sleep(.01);closing.cancel()
  with self.assertRaises(asyncio.CancelledError):await closing
  self.assertFalse(self.controller.closed.done());gate.set();await ta;await self.controller.close()
  self.assertIs(self.Stage.process,self.original_process)
 async def test_close_drains_before_restoring(self):
  gate=asyncio.Event();a=self.Event('A');a.extra['tts_gate']=gate
  ta=asyncio.create_task(self.run_event(a));await self.wait_tts('A');closing=asyncio.create_task(self.controller.close());await asyncio.sleep(.01)
  self.assertFalse(closing.done());gate.set();await ta;await closing
  self.assertIs(self.Stage.process,self.original_process);self.assertIs(self.Event.__dict__['send_message'],self.original_send)

if __name__=='__main__':unittest.main()
