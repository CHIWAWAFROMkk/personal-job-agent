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
test('manifest has only loopback hosts and click-authorized injection',()=>{
  const manifest=JSON.parse(source('manifest.json'));assert.equal(manifest.content_scripts,undefined);
  assert.deepEqual(manifest.host_permissions,['http://127.0.0.1:*/*','http://localhost:*/*']);assert.equal(manifest.permissions.includes('tabs'),false);
  assert.equal(source('fill.js').includes('fetch('),false);assert.equal(source('fill.js').includes('requestSubmit('),false);
});
test('verification code receiving and polling are removed',()=>{
  const f=fixture();
  for(const key of ['otpEligible','arm','start','stop','tick']) assert.equal(f.api[key],undefined);
  assert.equal(source('popup.html').includes('otpBtn'),false);
  for(const name of ['popup.js','fill.js','background.js']) {
    assert.equal(/BOUND_CODE|\/api\/fill\/poll|setInterval/.test(source(name)),false);
  }
});
test('background restricts local pairing credentials to trusted extension contexts',async()=>{
  let access;
  const c=vm.createContext({chrome:{storage:{local:{setAccessLevel:async value=>{access=value.accessLevel;}}}}});
  vm.runInContext(source('background.js'),c);
  assert.equal(access,'TRUSTED_CONTEXTS');
});
