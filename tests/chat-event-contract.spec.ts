import { expect, test } from '@playwright/test'
import { appendActivity, readActivityStream } from '../src/lib/agent-activity'

test('Structured tool events keep actual IDs, inputs and results without replaying tools', async () => {
 let blocks = appendActivity([], { type: 'step_start', id: 'tool-1', label: 'Check connections', command: 'check_connections({})', toolName: 'check_connections', args: { repair: false } })
 blocks = appendActivity(blocks, { type: 'step_end', id: 'tool-1', status: 'error', output: '{"error":"invalid pin"}', result: { error: 'invalid pin' } })
 expect(blocks).toEqual([{ type: 'step', id: 'tool-1', label: 'Check connections', command: 'check_connections({})', toolName: 'check_connections', args: { repair: false }, status: 'error', output: '{"error":"invalid pin"}', result: { error: 'invalid pin' } }])
 expect(() => appendActivity(blocks, { type: 'step_start', id: 'tool-1', label: 'Duplicate', command: 'noop()' })).toThrow('duplicate')
 expect(() => appendActivity([], { type: 'step_end', id: 'missing', status: 'success', output: 'ok' })).toThrow('matching start')
})
test('Successful backend completion accepts a null reason', async () => {
 const events: unknown[] = []
 await readActivityStream(new Response('{"type":"done","status":"success","reason":null,"changes":{},"successfulTools":{},"toolCalls":0,"modelCalls":1,"elapsedMs":127600}\n'), event => events.push(event))
 expect(events).toEqual([{ type: 'done', status: 'success', changes: {}, successfulTools: {}, toolCalls: 0, modelCalls: 1, elapsedMs: 127600 }])
})
test('Fragmented streams preserve terminal outcome and ignore content after done', async () => {
 const encoder = new TextEncoder(); const events: unknown[] = []
 const stream = new ReadableStream({ start(controller) { for (const part of ['{"type":"heart', 'beat"}\n{"type":"text","channel":"answer","text":"Ready"}\n', '{"type":"done","status":"awaiting_answers"}\n{"type":"text","text":"unexpected"}\n']) controller.enqueue(encoder.encode(part)); controller.close() } })
 await readActivityStream(new Response(stream), event => events.push(event))
 expect(events).toEqual([{ type: 'heartbeat' }, { type: 'text', channel: 'answer', text: 'Ready' }, { type: 'done', status: 'awaiting_answers' }])
})
for (const [body, error] of [
 ['{"type":"step_start","id":"x"}\n', 'invalid tool start'],
 ['{"type":"step_end","id":"x","status":"success"}\n', 'invalid tool result'],
 ['{"type":"done","status":"pretend_complete"}\n', 'unknown run outcome'],
 ['{"type":"text","text":"partial"}\n', 'ended before completion'],
 ['{"type":"step_start","id":"x","label":"Check","command":"check()"}\n{"type":"step_start","id":"x","label":"Check","command":"check()"}\n', 'duplicate'],
 ['{"type":"step_end","id":"missing","status":"success","output":"ok"}\n', 'matching start'],
 ['{"type":"step_start","id":"x","label":"Check","command":"check()"}\n{"type":"done","status":"success"}\n', 'before all tools completed'],
 ['{"type":"step_start","id":"x","label":"Check","command":"check()"}\n{"type":"step_end","id":"x","status":"success","output":"ok"}\n{"type":"step_end","id":"x","status":"success","output":"ok"}\n', 'more than once'],
] as const) test(`Stream rejects ${error}`, async () => {
 await expect(readActivityStream(new Response(body), () => {})).rejects.toThrow(error)
})
