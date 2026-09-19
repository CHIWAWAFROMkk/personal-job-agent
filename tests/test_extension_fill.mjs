import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import {readFileSync} from 'node:fs';
const root = new URL('../src/job_agent/extension/', import.meta.url);
const source = name => readFileSync(new URL(name,root),'utf8');
function fixture() {
  class Input {
    constructor(label, props={}) {Object.assign(this,{tagName:'INPUT',type:'text',labels:[{textContent:label}],attrs:{},style:{},isConnected:true,disabled:false,readOnly:false,hidden:false,maxLength:-1,events:[],_value:'',name:'',id:''},props);}
    get value(){return this._value;} set value(value){this._value=value;}
    getAttribute(key){return this.attrs[key] ?? null;} setAttribute(key,value){this.attrs[key]=value;}
    closest(){return null;} getBoundingClientRect(){return {width:200,height:44};} getClientRects(){return [1];}
    dispatchEvent(event){this.events.push(event.type);this.onEvent?.(event);}
  }
  class Textarea extends Input {constructor(label,props={}){super(label,{tagName:'TEXTAREA',...props});} get value(){return this._value;} set value(value){this._value=value;}}
  const inputs=[], messages=[];
  const context=vm.createContext({document:{querySelectorAll:()=>inputs,getElementById:()=>null,activeElement:null},HTMLInputElement:Input,HTMLTextAreaElement:Textarea,Event:class {constructor(type){this.type=type;}},getComputedStyle:el=>({display:'block',visibility:'visible',opacity:'1',...el.computed}),location:{href:'https://jobs.example.test/apply'},Date,setInterval:()=>1,clearInterval:()=>{},chrome:{runtime:{sendMessage:async msg=>{messages.push(msg);return context.reply || {code:null};}}}});
  vm.runInContext(source('fill.js'),context);
  return {context,api:context.PjaFill,Input,Textarea,inputs,messages};
}
test('local URL guard rejects remote, userinfo, path, query and fragments',()=>{
  const c=vm.createContext({URL});vm.runInContext(source('safety.js'),c);
  for(const raw of ['https://127.0.0.1:8787','http://127.0.0.1.evil.test:8787','http://evil.test','http://localhost@evil.test','http://user:pass@localhost','http://localhost/api','http://localhost?token=x','http://localhost/#x','file:///tmp/x']) assert.throws(()=>c.PjaSafety.localBase(raw),raw);
  assert.equal(c.PjaSafety.localBase('http://127.0.0.1:1234/'),'http://127.0.0.1:1234');
  assert.equal(c.PjaSafety.localBase('http://localhost:8787'),'http://localhost:8787');
  for(const raw of ['0','-1','1x','1/2','']) assert.throws(()=>c.PjaSafety.jobId(raw));
  for(const raw of ['http://jobs.example.test','https://jobs.example.test:8080','https://user:pass@jobs.example.test']) assert.throws(()=>c.PjaSafety.recruitmentUrl(raw));
  assert.equal(c.PjaSafety.recruitmentUrl('https://jobs.example.test:443/apply'),'https://jobs.example.test/apply');
});
test('objective unique fields map through labels and native setters',()=>{
  const {api,Input,Textarea,inputs}=fixture();
  for(const [label,key] of [['姓名','name'],['联系电话','phone'],['Email address','email'],['学校名称','school'],['专业','major'],['学位','degree']]) {const el=new Input(label);inputs.push(el);assert.equal(api.fieldKey(el),key);}
  const exp=new Textarea('经历概述');inputs.push(exp);assert.equal(api.fieldKey(exp),'experience');
  const data={name:'测试求职者',phone:'00000000000',email:'candidate@example.test',school:'示例院校',major:'示例专业',degree:'本科',experience:'示例机构：已确认事实'};
  const result=api.fill(data);assert.equal(result.filled.length,7);
  assert.equal(inputs[0].value,data.name);assert.deepEqual(inputs[0].events,[]);
});
test('ambiguous, prefilled, hidden, sensitive, password, file, selector and maxlength are skipped',()=>{
  const {api,Input,Textarea,inputs}=fixture();
  const samples=[new Input('姓名',{_value:'原值'}),new Input('手机',{hidden:true}),new Input('邮箱',{disabled:true}),new Input('学校',{readOnly:true}),new Input('姓名',{type:'password'}),new Input('姓名',{type:'file'}),new Input('姓名',{tagName:'SELECT'}),new Input('薪资'),new Input('期望城市'),new Input('紧急联系人姓名'),new Input('短信验证码'),new Input('name',{name:'captcha'}),new Textarea('工作描述'),new Input('姓名',{attrs:{'aria-label':'手机'}})];
  for(const el of samples) assert.equal(api.fieldKey(el),null);
  inputs.push(new Input('学校',{_value:'已有学校'}),new Input('学校'),new Input('专业',{maxLength:2}));
  assert.equal(api.fill({school:'示例院校',major:'长专业名称'}).filled.length,0);
  assert.equal(inputs[1].value,'');
});
test('silent prefill does not trigger a website onchange requestSubmit handler',()=>{
  const {api,Input,inputs}=fixture();const first=new Input('姓名'),second=new Input('手机');
  let submitted=false;first.onEvent=()=>{submitted=true;};inputs.push(first,second);
  assert.equal(api.fill({name:'示例',phone:'00000000000'}).filled.length,2);assert.equal(submitted,false);assert.deepEqual(first.events,[]);
});
test('OTP only binds focused empty SMS input, rejects CAPTCHA/split/occupied',async()=>{
  const f=fixture();
  for(const el of [new f.Input('验证码'),new f.Input('图形验证码',{attrs:{autocomplete:'one-time-code'}}),new f.Input('短信验证码',{maxLength:1}),new f.Input('短信验证码',{_value:'123456'})]) assert.equal(f.api.otpEligible(el),false);
  const el=new f.Input('短信验证码',{type:'tel'});f.context.document.activeElement=el;f.api.arm();f.api.start();f.context.reply={code:'123456'};await f.api.tick();
  assert.equal(el.value,'123456');assert.deepEqual(el.events,[]);assert.equal(f.messages[0].action,'POLL_BOUND_CODE');
});
test('OTP navigation, detached node and populated target stop without leaking code',async()=>{
  for(const mutate of [f=>{f.context.location.href+='?other';},f=>{f.context.document.activeElement.isConnected=false;},f=>{f.context.document.activeElement.value='manual';}]) {
    const f=fixture(),el=new f.Input('短信验证码');f.context.document.activeElement=el;f.api.arm();f.api.start();mutate(f);f.context.reply={code:'123456'};await f.api.tick();assert.notEqual(el.value,'123456');assert.equal(f.messages[0].action,'CLOSE_BOUND_CODE');
  }
});
test('OTP DOM replacement during async poll cannot fill old/new node',async()=>{
  const f=fixture(),el=new f.Input('短信验证码');f.context.document.activeElement=el;f.api.arm();f.api.start();
  f.context.chrome.runtime.sendMessage=async()=>{el.isConnected=false;return {code:'123456'};};await f.api.tick();assert.equal(el.value,'');
});
test('OTP timeout and unsupported code stop without dispatching any input events',async()=>{
  for(const kind of ['expired','invalid']) {
    const f=fixture(),el=new f.Input('短信验证码');f.context.document.activeElement=el;f.api.arm();f.api.start();
    if(kind==='expired') f.context.Date={now:()=>Date.now()+121000};
    f.context.reply={code:'not-a-code'};await f.api.tick();assert.equal(el.value,'');assert.deepEqual(el.events,[]);
  }
});
test('manifest has only loopback hosts and click-authorized injection',()=>{
  const manifest=JSON.parse(source('manifest.json'));assert.equal(manifest.content_scripts,undefined);
  assert.deepEqual(manifest.host_permissions,['http://127.0.0.1:*/*','http://localhost:*/*']);assert.equal(manifest.permissions.includes('tabs'),false);
  assert.equal(source('fill.js').includes('fetch('),false);assert.equal(source('fill.js').includes('requestSubmit('),false);
});
test('background binds sender tab URL document frame and never exposes token',async()=>{
  const storage={};let listener, calls=[];
  const local={serverUrl:'http://127.0.0.1:8787',agentToken:'synthetic-test-token'};
  const c=vm.createContext({URL,Date,Promise,AbortSignal,importScripts:()=>{},fetch:async(url,options)=>{calls.push({url,options});return {ok:true,json:async()=>url.endsWith('/session')?{session_id:'synthetic-session',origin:'https://jobs.example.test',expires_in:120}:url.endsWith('/poll')?{code:'123456'}:{ok:true}};},chrome:{runtime:{id:'test-extension',getURL:path=>'chrome-extension://test-extension/'+path,onMessage:{addListener:fn=>{listener=fn;}}},tabs:{get:async()=>({id:3,url:'https://jobs.example.test/apply'})},storage:{local:{get:async()=>local,setAccessLevel:async()=>{}},session:{get:async key=>({[key]:storage[key]}),set:async data=>Object.assign(storage,data),remove:async key=>{delete storage[key];},setAccessLevel:async()=>{}}}}});
  vm.runInContext(source('safety.js'),c);vm.runInContext(source('background.js'),c);
  const send=(request,sender)=>new Promise(resolve=>listener(request,sender,resolve));
  const start={action:'START_BOUND_CODE',tabId:3,url:'https://jobs.example.test/apply',documentId:'doc-1',jobId:5};
  const popup={id:'test-extension',url:'chrome-extension://test-extension/popup.html'};
  assert.equal(Boolean((await send(start,{...popup,id:'other-extension'})).error),true);
  assert.equal(Boolean((await send({...start,url:'http://jobs.example.test/apply'},popup)).error),true);
  assert.equal(calls.length,0);
  assert.equal((await send(start,popup)).ready,true);
  const sender={id:'test-extension',tab:{id:3},frameId:0,documentId:'doc-1',url:start.url};
  for(const bad of [{...sender,tab:{id:4}},{...sender,documentId:'doc-2'},{...sender,url:start.url+'?other'},{...sender,frameId:1}]) assert.equal((await send({action:'POLL_BOUND_CODE'},bad)).closed,true);
  assert.equal(calls.filter(x=>x.url.endsWith('/poll')).length,0);
  const result=await send({action:'POLL_BOUND_CODE'},sender);assert.equal(result.code,'123456');assert.equal(JSON.stringify(result).includes(local.agentToken),false);assert.equal(storage.otpBinding,undefined);
  assert.equal((await send({action:'POLL_BOUND_CODE'},sender)).closed,true);
  assert.equal(calls.every(x=>x.options.redirect==='error'),true);
  await send(start,popup);storage.otpBinding.expires=Date.now()-1;
  const before=calls.filter(x=>x.url.endsWith('/poll')).length;
  assert.equal((await send({action:'POLL_BOUND_CODE'},sender)).closed,true);
  assert.equal(calls.filter(x=>x.url.endsWith('/poll')).length,before);
  local.serverUrl='http://remote.example.test';
  assert.equal(Boolean((await send(start,popup)).error),true);
  assert.equal(calls.some(x=>x.url.includes('remote.example.test')),false);
});
