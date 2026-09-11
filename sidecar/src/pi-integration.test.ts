import assert from "node:assert/strict";
import { test } from "node:test";
import { createServer, type ServerResponse } from "node:http";
import { once } from "node:events";
import { spawn } from "node:child_process";
import { createInterface } from "node:readline";

test("real Pi SDK and runner: tools, scoped requests, history reset, provider error and cancellation", { timeout: 60000 }, async () => {
  const requests: any[] = [], bridges: any[] = [], notifications: any[] = [];
  let mode = "tool", held: ServerResponse | undefined;
  let arrival: (() => void) | undefined;
  const server = createServer(async (req, res) => {
    let raw = ""; for await (const chunk of req) raw += chunk;
    const body = JSON.parse(raw); requests.push(body);
    assert.equal(req.url, "/v1/chat/completions");
    if (mode === "error") {
      res.writeHead(401, {"Content-Type":"application/json"});
      res.end(JSON.stringify({error:{message:"synthetic invalid key", type:"authentication_error"}})); return;
    }
    res.writeHead(200, {"Content-Type":"text/event-stream"});
    const send = (delta: any, reason: string | null = null) => res.write("data: " + JSON.stringify({
      id:"chat-test", object:"chat.completion.chunk", created:1, model:"synthetic",
      choices:[{index:0, delta, finish_reason:reason}],
    }) + "\n\n");
    if (mode === "hold") { held = res; res.flushHeaders(); arrival?.(); return; }
    const hasTool = body.messages.some((m: any) => m.role === "tool");
    if (mode === "tool" && !hasTool) {
      send({role:"assistant", reasoning_content:"Synthetic reasoning"});
      send({role:"assistant", tool_calls:[{index:0,id:"call_1",type:"function",
        function:{name:"patch_state",arguments:JSON.stringify({ops:[{op:"set_topic", text:"Synthetic topic",evidence:["sg_first"]}]})}}]});
      send({}, "tool_calls");
    } else { send({role:"assistant",content:"Done"}); send({}, "stop"); }
    res.end("data: [DONE]\n\n");
  });
  server.listen(0, "127.0.0.1"); await once(server, "listening");
  const address = server.address() as {port:number};
  const child = spawn(process.execPath, [process.env.OPENWHISPER_TEST_SIDECAR_ENTRY!], {
    stdio:"pipe", env:{...process.env, OPENWHISPER_SIDECAR_TOKEN:"synthetic-token", OPENWHISPER_LLM_API_KEY:"synthetic-key"},
  });
  let stderr = ""; child.stderr.on("data", data => stderr += data);
  const pending = new Map<number, {resolve:(x:any)=>void, reject:(e:Error)=>void}>();
  let nextId = 1;
  const send = (value: any) => child.stdin.write(JSON.stringify(value)+"\n");
  const rpc = (method:string, params:any = {}) => new Promise<any>((resolve,reject) => {
    const id = nextId++; pending.set(id,{resolve,reject}); send({jsonrpc:"2.0",id,method,params});
  });
  const lines = createInterface({input:child.stdout});
  lines.on("line", line => {
    const message = JSON.parse(line);
    if (message.method?.startsWith("tool.")) {
      bridges.push(message);
      send({jsonrpc:"2.0",id:message.id,result:{results:message.params.ops.map((op:any)=>({op,ok:true}))}});
    } else if (message.id !== undefined) {
      const handler = pending.get(message.id); pending.delete(message.id);
      if (message.error) handler?.reject(new Error(message.error.message));
      else handler?.resolve(message.result);
    } else notifications.push(message);
  });
  child.once("exit", code => { for (const p of pending.values()) p.reject(new Error("child exit "+code+" "+stderr)); });
  const checkpoint = (id:string) => rpc("checkpoint", {request_id:id,state:{cards:{},topic:{}},
    new_segments:[{id:"sg_"+id,start_s:0,end_s:1,text:"Transcript "+id}]});
  try {
    await rpc("initialize", {meeting_id:"synthetic", provider:"local", kind:"openrouter", model:"deepseek-v4-synthetic",
      base_url:"http://127.0.0.1:"+address.port+"/v1", system_prompt:"MEETING_HOST_PROMPT",
      model_metadata:{protocol:"chat",reasoning:true}});
    assert.equal(notifications[0].method, "hello");
    assert.equal(notifications[0].params.token, "synthetic-token");
    const first = await checkpoint("first");
    assert.equal(first.applied, 1, JSON.stringify({first,bridges,notifications,stderr}));
    assert.equal(bridges.length, 1);
    assert.equal(bridges[0].method, "tool.patch_state");
    assert.equal(bridges[0].params.request_id, "first");
    const names = requests[0].tools.map((t:any)=>t.function.name).sort();
    assert.ok(names.includes("patch_state"));
    assert.ok(!names.some((name:string)=>["bash","read","write","edit"].includes(name)));
    assert.match(JSON.stringify(requests[0].messages), /MEETING_HOST_PROMPT/);
    assert.ok(requests[1].messages.some((m:any)=>m.role==="tool"));
    assert.ok(requests[1].messages.some((m:any)=>m.role==="assistant" && m.reasoning_content==="Synthetic reasoning"));
    mode = "plain";
    await checkpoint("second");
    assert.doesNotMatch(JSON.stringify(requests.at(-1).messages), /Transcript first/);
    mode = "error";
    await assert.rejects(checkpoint("failed"), /synthetic invalid key|401/);
    mode = "hold";
    const started = new Promise<void>(resolve => { arrival = resolve; });
    const active = checkpoint("cancel-me");
    await started;
    await rpc("cancel", {request_id:"cancel-me"});
    assert.equal((await active).canceled, true);
    held?.end(); mode = "plain";
    assert.equal((await checkpoint("after-cancel")).canceled, undefined);
    assert.ok(notifications.some(n=>n.method==="progress" && n.params.request_id==="first"));
    await rpc("shutdown");
    const [code] = await once(child, "exit"); assert.equal(code, 0, stderr);
  } finally {
    child.kill(); held?.end(); server.closeAllConnections(); server.close(); lines.close();
  }
});
