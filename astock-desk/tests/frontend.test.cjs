const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const {webcrypto}=require('node:crypto');
const path=require('node:path');
const html=fs.readFileSync(path.join(__dirname,'../web/index.html'),'utf8');
const script=html.split('<script>')[1].split('</script>')[0];

function app(){
  const elements=new Map(),storage=new Map(),requests=[];
  const element=key=>{
    if(!elements.has(key))elements.set(key,{
      value:'',innerHTML:'',textContent:'',style:{},dataset:{},disabled:false,
      classList:{add(){},remove(){},toggle(){}},addEventListener(){},
      getContext:()=>({scale(){},clearRect(){},beginPath(){},moveTo(){},lineTo(){},stroke(){},fillRect(){}})
    });
    return elements.get(key);
  };
  const context=vm.createContext({
    document:{querySelector:element,querySelectorAll:()=>[]},
    localStorage:{getItem:k=>storage.get(k)||null,setItem:(k,v)=>storage.set(k,String(v))},
    crypto:webcrypto,Intl,URL,devicePixelRatio:1,setTimeout(){},clearTimeout(){},setInterval(){},
    fetch:async(url,options)=>{
      requests.push({url,options});
      const data=url==='/api/config'?{llm:{primary:{configured:false},backup:{configured:false}}}:
        url==='/api/portfolio'?{positions:[],decisions:[]}:{};
      return {ok:true,headers:{get:()=> 'application/json'},json:async()=>data};
    },
    EventSource:class {addEventListener(){} close(){}},
  });
  vm.runInContext(script,context);
  return {context,element,storage,requests,run:code=>vm.runInContext(code,context)};
}

test('script initializes without dependencies or automatic AI calls',async()=>{
  const a=app();await new Promise(setImmediate);
  assert.equal(a.element('#llmBadge').textContent,'AI 未配置');
  assert.ok(!a.requests.some(r=>r.url.startsWith('/api/ai_picks')));
});

test('HTML and stored court score are escaped',()=>{
  const a=app();a.storage.set('court_score','<img src=x onerror=alert(1)>');
  a.run(`COURT={name:'<img src=x>',bull:[],bear:[]};renderCourt()`);
  assert.ok(!a.element('#courtBody').innerHTML.includes('<img'));
  assert.ok(a.element('#courtBody').innerHTML.includes('&lt;img'));
});

test('latest quote wins even when older response arrives later',async()=>{
  const a=app();await new Promise(setImmediate);
  const resolve={};
  a.context.fetch=(url)=>new Promise(done=>{resolve[url]=data=>done({ok:true,headers:{get:()=> 'application/json'},json:async()=>data});});
  const first=a.run(`loadQuote('600519')`),second=a.run(`loadQuote('000858')`);
  resolve['/api/quote?code=000858']({quote:{code:'000858',name:'new'},kline:[]});await second;
  resolve['/api/quote?code=600519']({quote:{code:'600519',name:'old'},kline:[]});await first;
  assert.equal(a.run('CUR.quote.code'),'000858');
});

test('failed new quote clears prior research and disables order button',async()=>{
  const a=app();await new Promise(setImmediate);
  a.run(`CUR={quote:{code:'600519'}};LAST_RESEARCH={code:'600519'}`);
  a.context.fetch=async()=>{throw new Error('offline');};
  await a.run(`loadQuote('000858')`);
  assert.equal(a.run('CUR'),null);assert.equal(a.run('LAST_RESEARCH'),null);
  assert.equal(a.element('#orderBtn').disabled,true);
});

test('API helper preserves content type with custom headers and surfaces business errors',async()=>{
  const a=app();await new Promise(setImmediate);let headers;
  a.context.fetch=async(url,options)=>{
    headers=options.headers;
    return {ok:true,headers:{get:()=> 'application/json'},json:async()=>({error:'bad request'})};
  };
  await assert.rejects(a.run(`api('/test',{headers:{'X-Test':'yes'}})`),/bad request/);
  assert.equal(headers['Content-Type'],'application/json');
  assert.equal(headers['X-Test'],'yes');
});

test('retry after network failure uses the same order identifier',async()=>{
  const a=app();await new Promise(setImmediate);
  a.run(`openOrder('buy','600519')`);
  for(const [key,value] of Object.entries({orderCode:'600519',orderQty:'100',a1:'specific evidence',a2:'contrary evidence',a3:'risk discipline'}))a.element('#'+key).value=value;
  a.element('#submitOrder').dataset={side:'buy',max:'0'};
  const orders=[];
  a.context.fetch=async(url,options)=>{orders.push(JSON.parse(options.body));throw new Error('connection lost');};
  await a.run('submitOrder()');await a.run('submitOrder()');
  assert.equal(orders.length,2);
  assert.equal(orders[0].request_id,orders[1].request_id);
  a.element('#orderQty').value='200';await a.run('submitOrder()');
  assert.equal(orders.length,2,'changing an uncertain order must not silently create another fill');
});
