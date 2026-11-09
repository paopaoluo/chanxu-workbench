/* Chanxu: local research only, no broker or order connections. */
(() => {
  'use strict';
  const $ = id => document.getElementById(id), PREF_KEY = 'chanxu.preferences.v1';
  const FIELDS = { initialCapital: 'capital', positionPct: 'position', riskPct: 'risk', lotSize: 'lot', feeBps: 'fee', slippageBps: 'slippage' };
  const colors = {up:'#ee8d80',down:'#79c8ad',amber:'#e2ab58',muted:'#a9b6aa',grid:'rgba(198,202,185,.13)'};
  const state = {bars:ChanEngine.sampleData(240),analysis:null,backtest:null,source:{kind:'sample',code:'DEMO',name:'合成示例',label:'合成示例 · 日线',currency:'',meta:{}},window:120,end:null,selectedSignal:null,datasetId:'',runId:'',precision:3,view:'chart',requestId:0,controller:null,layers:{fractals:true,pens:true,segments:true,centers:true,signals:true}};
  const clock = {status:null,serverEpoch:0,syncedAt:0,pending:false,error:'',requestActive:false,lastAutoAttempt:-Infinity,lastAutoTarget:'',autoPausedUntil:0,lastBoundary:''};
  const AUTO_RETRY_MS = 120000;
  const PHASE_LABELS = {preopen:'开盘前',morning:'早盘交易中',lunch:'午间休市',afternoon:'午盘交易中',closed:'已收盘',non_trading_day:'非交易日'};
  const daily = {payload:null,loading:false,error:'',type:'all',query:'',clean:false,page:1,pageSize:10,snapshotJobId:null};
  const watch = {payload:null,loading:false,saving:false,error:'',message:'',readId:0,query:'',searchId:0,searchController:null,searchTimer:null,searchLoading:false,searchError:'',results:[],searchMeta:null};
  const watchMonitor = {summaries:{},queue:[],active:new Set(),generation:0,targetDate:'',lastAttempt:{},paused:false,notice:''};
  const symbolSearch = {query:'',results:[],meta:null,loading:false,error:'',open:false,activeIndex:-1,id:0,controller:null,timer:null,composing:false,editing:false,inputRevision:0};
  const fmt = (v,d=2) => Number.isFinite(v) ? new Intl.NumberFormat('zh-CN',{minimumFractionDigits:d,maximumFractionDigits:d}).format(v) : '—';
  const price = v => fmt(v,state.precision);
  const pct = v => Number.isFinite(v) ? (v>=0?'+':'')+(v*100).toFixed(2)+'%' : '—';
  const date = v => String(v||'').slice(0,10);
  const esc = v => String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'})[c]);
  const money = v => fmt(v)+' '+(typeof state.source.currency==='string'&&/^[A-Z]{3}$/.test(state.source.currency)?state.source.currency:state.source.kind==='sample'?'模拟单位':'报价单位');
  const signalLabel = t => ({B1:'一买 · 试错',B2:'二买 · 回调',B3:'三买 · 回抽',S1:'一卖 · 背驰',S2:'二卖 · 反弹',S3:'三卖 · 回抽'})[t]||t;
  const exitReason = r => ({'structure-stop':'结构止损','sell-signal':'卖点','same-bar-conservative-stop':'同K保守止损','deferred-structure-stop':'T+1延迟止损','deferred-sell-signal':'T+1延迟卖点'})[r]||r||'策略退出';

  function toast(text){$('toast').textContent=text;$('toast').classList.add('show');clearTimeout(toast.timer);toast.timer=setTimeout(()=>$('toast').classList.remove('show'),4200);}
  function requestStatus(text,kind='info'){$('request-status').textContent=text;$('request-status').className='request-status '+kind;$('data-indicator').className=kind==='error'?'error':state.source.meta?.stale?'warning':'';}
  function isChinaOnline(){return state.source.kind==='online'&&(/\.(SS|SZ)$/.test(state.source.code)||state.source.code==='^SSEC');}
  function serverNow(){return clock.status?clock.serverEpoch+Math.max(0,performance.now()-clock.syncedAt):null;}
  function cnParts(epoch){
    const shifted=new Date(epoch+8*3600000);
    return {day:shifted.toISOString().slice(0,10),time:shifted.toISOString().slice(11,19),afterClose:shifted.getUTCHours()>=15};
  }
  function clockBoundary(epoch){const p=cnParts(epoch);return p.day+':'+(p.afterClose?'closed':'before-close');}
  function acceptMarketStatus(value,fromHistory=false){
    if(!value||!Number.isFinite(Date.parse(value.serverTime))||!/^\d{4}-\d{2}-\d{2}$/.test(value.targetDate)||value.timezone!=='Asia/Shanghai')throw new Error('服务端交易时钟格式不正确');
    // A history request may finish after a newer clock poll. Never move the
    // close boundary backwards because that older response arrived last.
    if(fromHistory&&clock.status&&Date.parse(value.serverTime)<clock.serverEpoch)return false;
    clock.status=value;clock.serverEpoch=Date.parse(value.serverTime);clock.syncedAt=performance.now();clock.error='';clock.lastBoundary=clockBoundary(clock.serverEpoch);scheduleWatchMonitor();
    return true;
  }
  function completionState(){
    const actual=date(state.bars.at(-1)?.time),target=clock.status?.targetDate||'',meta=state.source.meta||{};
    return {actual,target,chinaOnline:isChinaOnline(),ready:isChinaOnline()&&!!target&&actual===target&&meta.readiness==='ready'&&!meta.stale&&!clock.error};
  }
  function renderMarketClock(){
    const now=serverNow(),s=clock.status,c=completionState(),meta=state.source.meta||{};
    $('market-clock').textContent=now===null?'时间同步不可用':cnParts(now).day+' '+cnParts(now).time;
    $('market-phase').textContent=s?(PHASE_LABELS[s.phase]||'交易状态待核验')+(clock.error?' · 校时失败':''):'交易状态未知';
    $('market-target').textContent=s?.targetDate||'待服务器判定';
    $('market-actual').textContent=c.actual+(state.source.kind==='sample'?' · 示例':state.source.kind==='csv'?' · CSV':state.source.kind==='snapshot'?' · 快照':'');
    $('session-b-points').textContent='—';
    let note,kind='neutral';
    if(state.source.kind==='snapshot'&&meta.reportTargetDate){
      const buys=(state.analysis?.signals||[]).filter(signal=>signal.type.startsWith('B')&&date(state.bars[signal.index]?.time)===meta.reportTargetDate);
      $('session-b-points').textContent='清单 '+meta.reportTargetDate+'：'+(buys.length?buys.map(signal=>signal.type).join(' / '):'无新 B 点');
      note='已载入 '+meta.reportTargetDate+' 报告冻结日K，不自动更新'+(s&&meta.reportTargetDate!==s.targetDate?'；这不是当前目标日清单':'');kind=s&&meta.reportTargetDate!==s.targetDate?'warning':'neutral';
    }
    else if(!c.chinaOnline){note=state.source.kind==='online'?'当前为非A股，沿用其行情完成度规则':'本地/示例数据不自动拉取，也不以A股时钟确认';}
    else if(clock.error||!s){note='交易时间核验不可用，保留原图表，不判定目标日新信号';kind='warning';}
    else if(meta.stale){note='过期缓存 · 未确认最新完整日K，将自动重试';kind='warning';$('session-b-points').textContent='待行情更新';}
    else if(!c.ready){note='等待数据源发布 '+c.target+' 完整日K；当前仅计算至 '+c.actual;kind='warning';$('session-b-points').textContent='待完整日K';}
    else{
      note='已取得 '+c.target+' 完整日K';kind='ready';
      const buys=(state.analysis?.signals||[]).filter(signal=>signal.type.startsWith('B')&&date(state.bars[signal.index]?.time)===c.target);
      $('session-b-points').textContent=buys.length?buys.map(signal=>signal.type).join(' / '):'无新 B 点';
    }
    $('market-data-ready').textContent=note;$('market-data-ready').className=kind;
    $('market-sync-status').textContent=clock.error?'校时失败：'+clock.error:s?'每分钟校时 · 待更新行情每2分钟重试':'等待本机服务；不使用浏览器时区猜交易日';
  }
  function maybeAutoRefresh(reason='poll',previousTarget='',previousDay=''){
    const c=completionState(),now=performance.now();
    if(!c.chinaOnline||!clock.status||clock.error||clock.requestActive||symbolSearch.editing||document.hidden||now<clock.autoPausedUntil)return false;
    const dayChanged=!!previousDay&&previousDay!==cnParts(clock.serverEpoch).day;
    const targetChanged=!!previousTarget&&previousTarget!==c.target;
    if(c.ready&&!dayChanged&&!targetChanged)return false;
    if(!dayChanged&&!targetChanged&&now-clock.lastAutoAttempt<AUTO_RETRY_MS)return false;
    clock.lastAutoAttempt=now;clock.lastAutoTarget=c.target;
    fetchOnline(null,{automatic:true,refresh:true,reason});return true;
  }
  async function syncMarketStatus(reason='poll'){
    if(clock.pending||location.protocol==='file:')return;
    clock.pending=true;const controller=new AbortController(),timeout=setTimeout(()=>controller.abort(),10000);
    const previousTarget=clock.status?.targetDate||'',previousDay=clock.status?cnParts(clock.serverEpoch).day:'';
    try{
      const response=await fetch('/api/market-status',{cache:'no-store',signal:controller.signal}),payload=await response.json();
      if(!response.ok)throw new Error(payload.error||'交易时钟服务不可用');
      acceptMarketStatus(payload);renderMarketClock();renderDailyList();maybeAutoRefresh(reason,previousTarget,previousDay);
    }catch(error){clock.error=error.name==='AbortError'?'同步超时':error.message==='Failed to fetch'?'本机服务连接失败':error.message;renderMarketClock();refreshWatchMonitorView();}
    finally{clearTimeout(timeout);clock.pending=false;}
  }
  function tickMarketClock(){
    renderMarketClock();const now=serverNow();if(now===null||document.hidden)return;
    const boundary=clockBoundary(now);if(boundary!==clock.lastBoundary){refreshWatchMonitorView();clock.lastBoundary=boundary;syncMarketStatus('boundary');}
  }
  function watchClockVerified(){const now=serverNow();return !!clock.status?.targetDate&&!clock.error&&now!==null&&clockBoundary(now)===clockBoundary(clock.serverEpoch);}
  function monitorAvailable(){return typeof WatchlistMonitor!=='undefined';}
  function monitorComplete(summary,target){return !!summary&&summary.targetDate===target&&!summary.uiState&&!summary.sourceStale&&(summary.ready||summary.status==='foreign');}
  function refreshWatchMonitorView(){if(monitorAvailable())renderWatchlist();}
  function pruneWatchMonitor(){
    const allowed=new Set((watch.payload?.items||[]).map(i=>i.symbol));watchMonitor.queue=watchMonitor.queue.filter(job=>allowed.has(job.symbol));
    for(const job of watchMonitor.active)if(!allowed.has(job.symbol)){job.discarded=true;job.controller.abort();}
    for(const symbol of Object.keys(watchMonitor.summaries))if(!allowed.has(symbol)){delete watchMonitor.summaries[symbol];delete watchMonitor.lastAttempt[symbol];}
  }
  function stopWatchMonitor(paused=true){
    watchMonitor.generation++;watchMonitor.queue=[];watchMonitor.paused=paused;watchMonitor.notice=paused?'已取消本轮读取；已显示的旧值保留，点击更新可重试。':'';
    for(const job of watchMonitor.active)job.controller.abort();
    for(const [symbol,summary] of Object.entries(watchMonitor.summaries))if(summary.uiState==='loading'||summary.uiState==='queued')watchMonitor.summaries[symbol]={...summary,ready:false,isNewBuy:false,newBuySignals:[],uiState:'cancelled',uiMessage:paused?'读取已取消，旧值待更新':'目标日已变化，旧值待更新'};
    refreshWatchMonitorView();
  }
  function scheduleWatchMonitor(options={}){
    if(!monitorAvailable())return;
    const target=clock.status?.targetDate||'',changed=target!==watchMonitor.targetDate;
    if(changed){stopWatchMonitor(false);watchMonitor.targetDate=target;}
    pruneWatchMonitor();if(options.force){watchMonitor.paused=false;watchMonitor.notice='';}
    if(!watchClockVerified()||document.hidden||watchMonitor.paused){refreshWatchMonitorView();return;}
    const now=performance.now();
    for(const item of watch.payload?.items||[]){
      const symbol=item.symbol,summary=watchMonitor.summaries[symbol];
      if(watchMonitor.queue.some(j=>j.symbol===symbol&&j.generation===watchMonitor.generation)||[...watchMonitor.active].some(j=>!j.discarded&&j.symbol===symbol&&j.generation===watchMonitor.generation))continue;
      if(!options.force&&monitorComplete(summary,target))continue;
      if(!options.force&&!changed&&now-(watchMonitor.lastAttempt[symbol]??-Infinity)<AUTO_RETRY_MS)continue;
      watchMonitor.summaries[symbol]={...(summary||{}),symbol,ready:false,isNewBuy:false,newBuySignals:[],uiState:'queued',uiMessage:summary?.close!=null?'等待更新，显示上次结果':'等待首次读取'};
      watchMonitor.queue.push({symbol,targetDate:target,generation:watchMonitor.generation,previous:summary});
    }
    refreshWatchMonitorView();pumpWatchMonitor();
  }
  function pumpWatchMonitor(){
    if(!monitorAvailable()||document.hidden||!watchClockVerified()||watchMonitor.paused)return;
    while(watchMonitor.active.size<2&&watchMonitor.queue.length){
      const job=watchMonitor.queue.shift();if(job.generation!==watchMonitor.generation||job.targetDate!==clock.status.targetDate||!inWatchlist(job.symbol))continue;
      job.controller=new AbortController();watchMonitor.active.add(job);watchMonitor.lastAttempt[job.symbol]=performance.now();
      watchMonitor.summaries[job.symbol]={...watchMonitor.summaries[job.symbol],uiState:'loading',uiMessage:job.previous?.close!=null?'正在更新，显示上次结果':'正在读取完整日K与计算信号'};
      runWatchMonitorJob(job);
    }
    refreshWatchMonitorView();
  }
  function monitorJobCurrent(job){return !job.discarded&&job.generation===watchMonitor.generation&&job.targetDate===clock.status?.targetDate&&inWatchlist(job.symbol)&&!job.controller.signal.aborted;}
  function monitorFailure(job,message,result){
    const previous=job.previous||result||{};return {...previous,symbol:job.symbol,targetDate:previous.targetDate||job.targetDate,attemptTargetDate:job.targetDate,ready:false,isNewBuy:false,newBuySignals:[],uiState:'error',uiMessage:message+(previous.close!=null?'；保留旧值，尚未确认最新结果':''),status:result?.status||'request_error'};
  }
  async function runWatchMonitorJob(job){
    let timedOut=false;const timeout=setTimeout(()=>{timedOut=true;if(!job.discarded&&job.generation===watchMonitor.generation&&job.targetDate===clock.status?.targetDate&&inWatchlist(job.symbol))watchMonitor.summaries[job.symbol]=monitorFailure(job,'行情读取超时');job.controller.abort();refreshWatchMonitorView();},20000);
    try{const response=await fetch('/api/history?'+new URLSearchParams({symbol:job.symbol,range:'2y',refresh:'1'}),{cache:'no-store',signal:job.controller.signal}),payload=await response.json();if(!monitorJobCurrent(job))return;if(!response.ok)throw new Error(payload.error||'行情服务返回 '+response.status);
      const result=WatchlistMonitor.analyzeHistory(job.symbol,payload,job.targetDate,bars=>ChanEngine.analyze(bars));if(!monitorJobCurrent(job))return;
      watchMonitor.summaries[job.symbol]=['invalid_data','insufficient_history','analysis_error'].includes(result.status)?monitorFailure(job,result.message||'数据或分析未通过校验',result):{...result,sourceStale:payload.meta?.stale===true,uiState:'',uiMessage:''};
    }catch(error){if(!job.discarded&&job.generation===watchMonitor.generation&&job.targetDate===clock.status?.targetDate&&inWatchlist(job.symbol)&&(!job.controller.signal.aborted||timedOut))watchMonitor.summaries[job.symbol]=monitorFailure(job,timedOut?'行情读取超时':error.message==='Failed to fetch'?'行情连接失败':error.message);}
    finally{clearTimeout(timeout);watchMonitor.active.delete(job);refreshWatchMonitorView();pumpWatchMonitor();}
  }
  function monitorCard(item){
    const summary=watchMonitor.summaries[item.symbol],target=clock.status?.targetDate||'',fresh=monitorAvailable()&&WatchlistMonitor.isFreshBuy(summary,target,watchClockVerified()),old=summary&&summary.targetDate!==target;
    const latest=summary?.latestSignals||[],hasClose=Number.isFinite(summary?.close),pending=summary?.uiState==='loading'||summary?.uiState==='queued';
    let message=!watchClockVerified()?'交易时钟未核验，不判定目标日新 B':!summary?'等待读取，不代表无信号':summary.uiMessage||(old?'旧目标日结果，等待更新':summary.message||'');
    const tags=latest.length?latest.map(s=>'<span class="watch-signal '+(s.type.startsWith('B')?'buy':'sell')+'">'+esc(s.type)+'</span>').join(' '):pending||summary?.uiState||!summary?'信号待计算':['ready','foreign','stale','awaiting_source'].includes(summary.status)?'当前历史内暂无确认信号':'信号未确认';
    return '<article class="watch-card monitor-card'+(fresh?' fresh-buy':'')+'" data-watch-card="'+esc(item.symbol)+'"><div class="watch-card-top"><div><strong>'+esc(item.name)+'</strong><small>'+esc(item.code)+' · '+esc(item.symbol)+'</small></div>'+(fresh?'<span class="watch-new-buy">'+esc(target)+' 新 B</span>':'')+'</div><div class="watch-quote"><b>'+(hasClose?fmt(summary.close,/^5\d{5}\.SS$/.test(item.symbol)?3:2):'—')+'</b><span>'+esc(summary?.currency||'')+'</span><small>完整日K收盘 · '+esc(summary?.closeDate||'待读取')+(summary?.uiState||old?' · 旧值/待更新':'')+'</small></div><div class="watch-signals"><span>最近确认买卖信号</span><div>'+tags+'</div><small>确认日 '+esc(summary?.latestSignalDate||'—')+'</small></div><p class="watch-card-state'+(summary?.uiState==='error'?' error':'')+'">'+esc(message)+'</p><div class="watch-card-actions"><button class="button button-quiet" title="查看同口径近2年日K" data-watch-open="'+esc(item.symbol)+'">查看K线</button><button class="button button-quiet" data-watch-remove="'+esc(item.symbol)+'" '+(watch.loading||watch.saving?'disabled':'')+' aria-label="移除自选 '+esc(item.name)+'">移除</button></div></article>';
  }
  function renderWatchMonitorSummary(items){
    const target=clock.status?.targetDate||'',verified=watchClockVerified(),values=items.map(i=>watchMonitor.summaries[i.symbol]),updated=values.filter(s=>monitorComplete(s,target)).length,failed=values.filter(s=>s?.uiState==='error'||s?.sourceStale).length,newBuys=monitorAvailable()?values.filter(s=>WatchlistMonitor.isFreshBuy(s,target,verified)).length:0;
    $('watch-monitor-target').textContent=target||'待校时';$('watch-monitor-updated').textContent=updated+' / '+items.length;$('watch-monitor-failed').textContent=String(failed);$('watch-monitor-buys').textContent=verified?String(newBuys):'待核验';
    $('watch-monitor-note').textContent='固定近2年日K · '+(!monitorAvailable()?'行情概览模块未载入，请刷新页面。':!verified?'交易时钟校验不可用：保留历史值，暂停目标日新 B 高亮。':watchMonitor.notice||((watchMonitor.active.size||watchMonitor.queue.length)?'正在逐只更新 · 并发最多 2 只；未完成不表示无 B 点。':'新 B 按目标交易日收盘确认日判断，不是极值日；非 A 股只展示，不参与新 B 置顶。'));
    $('watch-monitor-refresh').disabled=!watch.payload||!items.length||!verified;$('watch-monitor-cancel').hidden=!watchMonitor.active.size&&!watchMonitor.queue.length;
  }
  function preferredName(meta,symbol){return [meta.nameZh,meta.chineseName,meta.displayName,meta.name,symbol].find(v=>typeof v==='string'&&v.trim())||symbol;}
  function currentWatchSymbol(){return ['online','snapshot'].includes(state.source.kind)&&/^(?:\d{6}\.(?:SS|SZ)|\^[A-Z][A-Z0-9._-]*|[A-Z][A-Z0-9.^=-]{0,19})$/.test(state.source.code||'')?state.source.code:null;}
  function inWatchlist(symbol){return !!watch.payload?.items.some(item=>item.symbol===symbol);}
  function validateWatchlist(payload){if(payload?.schema!=='chanxu-watchlist-v1'||payload.storage!=='local_server'||!Number.isInteger(payload.revision)||payload.revision<0||!Array.isArray(payload.items)||!payload.items.every(i=>i&&typeof i.symbol==='string'&&typeof i.code==='string'&&typeof i.name==='string')||new Set(payload.items.map(i=>i.symbol)).size!==payload.items.length)throw new Error('自选数据格式不正确');return payload;}
  function watchButton(symbol,label){const selected=inWatchlist(symbol),disabled=!watch.payload||watch.loading||watch.saving;return '<button class="button button-quiet watch-toggle" data-watch-symbol="'+esc(symbol)+'" aria-label="'+esc((selected?'取消自选 ':'加入自选 ')+(label||symbol))+'" aria-pressed="'+selected+'" '+(disabled?'disabled':'')+'>'+(selected?'已在自选':'加入自选')+'</button>';}
  function renderWatchSearch(){
    $('watchlist-search-status').textContent=watch.searchLoading?'正在搜索证券目录…':watch.searchError?'搜索失败：'+watch.searchError:!watch.query.trim()?'输入代码或中文名搜索后添加，不预填自选。':watch.results.length?'找到 '+watch.results.length+' 项 · '+(watch.searchMeta?.source||'本机证券目录')+(watch.searchMeta?.snapshotDate?' · 名称快照 '+watch.searchMeta.snapshotDate:''):'没有匹配证券，请尝试完整代码或更短的中文名。';
    $('watchlist-results').innerHTML=watch.results.map(item=>'<div class="watch-result"><div><strong>'+esc(item.name)+'</strong><small>'+esc(item.code)+' · '+esc(item.symbol)+'</small></div>'+(item.historySupported===false?'<span class="watch-unsupported">暂不支持行情<small>'+esc(item.reason||'此证券目前不能加入可用行情自选')+'</small></span>':watchButton(item.symbol,item.name))+'</div>').join('');
  }
  function renderWatchlist(){
    const originalItems=watch.payload?.items||[],items=monitorAvailable()?WatchlistMonitor.sortItems(originalItems,watchMonitor.summaries,clock.status?.targetDate||'',watchClockVerified()):originalItems,symbol=currentWatchSymbol(),toggle=$('chart-watch-toggle');
    $('watchlist-count').textContent=watch.payload?String(items.length):'—';
    $('watchlist-status').textContent=watch.saving?'正在保存到本机服务，成功前不会改变自选…':watch.loading?'正在读取本机自选…':watch.error?'自选操作失败：'+watch.error+(watch.payload?'。保留已确认的自选记录。':'。尚未确认自选内容，请刷新重试。'):watch.message||(!watch.payload?'尚未读取本机自选。':!items.length?'还没有自选股。搜索添加，或从图表、新 B 点清单加入。':'已保存在本机 · '+items.length+' 项；目标日新 B 置顶，其它保留添加顺序。');
    $('watchlist-status').className='watchlist-status'+(watch.error?' error':'');$('watchlist-refresh').disabled=watch.loading||watch.saving;
    $('watchlist-items').innerHTML=items.map(monitorCard).join('');renderWatchMonitorSummary(originalItems);
    toggle.textContent=symbol&&inWatchlist(symbol)?'已在自选':'加入自选';toggle.disabled=!symbol||!watch.payload||watch.loading||watch.saving;toggle.setAttribute('aria-pressed',String(!!symbol&&inWatchlist(symbol)));toggle.title=!symbol?'示例及无证券代码的本地数据不能收藏':inWatchlist(symbol)?'点击取消自选':'保存到本机自选';
    renderWatchSearch();
  }
  async function loadWatchlist(options={}){
    if(watch.loading||watch.saving&&!options.allowDuringWrite)return false;
    const id=++watch.readId,controller=new AbortController(),timeout=setTimeout(()=>controller.abort(),15000);watch.loading=true;watch.error='';watch.message='';renderWatchlist();renderDailyList();
    try{const response=await fetch('/api/watchlist',{cache:'no-store',signal:controller.signal}),payload=await response.json();if(id!==watch.readId)return false;if(!response.ok)throw new Error(payload.error||'读取自选失败');validateWatchlist(payload);if(!watch.payload||payload.revision>=watch.payload.revision)watch.payload=payload;scheduleWatchMonitor({force:!!options.refreshMonitor});return true;}
    catch(error){if(id===watch.readId)watch.error=error.name==='AbortError'?'读取超时':error.message;return false;}
    finally{clearTimeout(timeout);if(id===watch.readId)watch.loading=false;renderWatchlist();renderDailyList();}
  }
  async function updateWatchlist(action,symbol){
    if(watch.saving||watch.loading)return false;if(!watch.payload){watch.error='请先成功读取自选后再操作';renderWatchlist();return false;}
    if(action==='add'&&inWatchlist(symbol)||action==='remove'&&!inWatchlist(symbol))return false;
    watch.saving=true;watch.error='';watch.message='';renderWatchlist();renderDailyList();const controller=new AbortController(),timeout=setTimeout(()=>controller.abort(),15000);
    try{const response=await fetch('/api/watchlist',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action,symbol,expectedRevision:watch.payload.revision}),signal:controller.signal}),payload=await response.json();
      if(response.status===409){const refreshed=await loadWatchlist({allowDuringWrite:true});watch.error=payload.code==='revision_conflict'?(refreshed?'其他页面已修改自选，已重新读取；本次操作未执行，请重试':'自选版本冲突且重新读取失败，请刷新后重试'):(payload.error||'自选当前状态不允许写入')+(refreshed?'；已重新读取自选，请处理提示后重试':'；重新读取也未成功，请处理提示后刷新重试');return false;}
      if(!response.ok)throw new Error(payload.error||'保存自选失败');validateWatchlist(payload);if(payload.revision<watch.payload.revision)throw new Error('服务返回了较旧的自选版本');watch.payload=payload;pruneWatchMonitor();scheduleWatchMonitor();watch.message=(action==='add'?'已加入':'已移除')+'自选 · 保存到本机服务成功';return true;
    }catch(error){watch.error=error.name==='AbortError'?'保存超时，结果尚未确认，请刷新自选核对后重试':error.message;return false;}
    finally{clearTimeout(timeout);watch.saving=false;renderWatchlist();renderDailyList();if(watch.error)toast('自选未确认保存：'+watch.error);else if(watch.message)toast(watch.message);}
  }
  function toggleWatchlist(symbol){return updateWatchlist(inWatchlist(symbol)?'remove':'add',symbol);}
  function queueWatchSearch(query){clearTimeout(watch.searchTimer);watch.searchController?.abort();watch.searchId++;watch.query=String(query).trim();watch.results=[];watch.searchMeta=null;watch.searchError='';watch.searchLoading=!!watch.query;renderWatchSearch();if(watch.query)watch.searchTimer=setTimeout(()=>searchWatchlist(watch.query),300);}
  async function searchWatchlist(query){
    clearTimeout(watch.searchTimer);watch.searchController?.abort();const id=++watch.searchId;watch.query=String(query).trim();watch.results=[];watch.searchError='';if(!watch.query){watch.searchLoading=false;renderWatchSearch();return;}
    const controller=new AbortController();watch.searchController=controller;watch.searchLoading=true;renderWatchSearch();const timeout=setTimeout(()=>controller.abort(),12000);
    try{const response=await fetch('/api/instruments?'+new URLSearchParams({query:watch.query}),{cache:'no-store',signal:controller.signal}),payload=await response.json();if(id!==watch.searchId)return;if(!response.ok)throw new Error(payload.error||'证券搜索不可用');if(!Array.isArray(payload.items)||!payload.items.every(i=>i&&typeof i.symbol==='string'&&typeof i.code==='string'&&typeof i.name==='string'))throw new Error('证券搜索格式不正确');watch.results=payload.items;watch.searchMeta=payload;}
    catch(error){if(id===watch.searchId)watch.searchError=error.name==='AbortError'?'搜索超时或已取消':error.message;}
    finally{clearTimeout(timeout);if(id===watch.searchId){watch.searchLoading=false;watch.searchController=null;renderWatchSearch();}}
  }
  async function openWatchChart(symbol){const item=watch.payload?.items.find(i=>i.symbol===symbol);if(!item)return;$('symbol-input').value=item.symbol;$('range-input').value='2y';const loaded=await fetchOnline();if(loaded&&state.source.code===item.symbol)navigate('chart');}
  function dailyCandidates(report=daily.payload?.report){
    const byCode=new Map();
    for(const row of report?.candidates||[]){
      if(!byCode.has(row.code))byCode.set(row.code,{...row,signals:[],flags:[]});
      const merged=byCode.get(row.code);
      for(const signal of row.signals||[]){const key=JSON.stringify([signal.type,signal.confirmationDate,signal.price,signal.stop,signal.id]);if(!merged.signals.some(s=>JSON.stringify([s.type,s.confirmationDate,s.price,s.stop,s.id])===key))merged.signals.push(signal);}
      merged.flags=[...new Set([...merged.flags,...(row.flags||[])])];
    }
    return [...byCode.values()];
  }
  function filteredDailyCandidates(){const query=daily.query.trim().toLocaleLowerCase('zh-CN');return dailyCandidates().filter(c=>(daily.type==='all'||c.signals.some(s=>s.type===daily.type))&&(!query||(String(c.code)+' '+c.name).toLocaleLowerCase('zh-CN').includes(query))&&(!daily.clean||!c.flags.length));}
  function dailyPage(){const rows=filteredDailyCandidates(),pages=Math.max(1,Math.ceil(rows.length/daily.pageSize));daily.page=Math.min(pages,Math.max(1,daily.page));return {rows,pages,visible:rows.slice((daily.page-1)*daily.pageSize,daily.page*daily.pageSize)};}
  function beijingTime(value){const epoch=Date.parse(value);return Number.isFinite(epoch)?new Date(epoch).toLocaleString('zh-CN',{hour12:false,timeZone:'Asia/Shanghai'}):'—';}
  function dailyReportState(){
    const p=daily.payload,r=p?.report,target=clock.status?.targetDate||p?.expectedTargetDate||'';
    if(!r)return p?.reportState||'missing';
    if(p.reportState==='calendar_unavailable'||p.reportState==='invalid')return p.reportState;
    return target&&r.targetDate!==target?'outdated':p.reportState;
  }
  function renderDailyList(){
    const p=daily.payload,r=p?.report,rows=dailyCandidates(),page=dailyPage(),coverage=r?.coverage||{},status=dailyReportState();
    const missing=(coverage.failed||0)+(coverage.unsupported||0),partial=r&&(r.status==='partial'||missing>0);
    $('daily-report-date').textContent=r?.targetDate||'—';$('daily-generated').textContent=r?beijingTime(r.generatedAt)+' 北京时间':'—';$('daily-target-date').textContent=clock.status?.targetDate||p?.expectedTargetDate||'待判定';
    $('daily-candidate-count').textContent=r?rows.length:'—';$('daily-signal-count').textContent=r?rows.reduce((n,c)=>n+c.signals.length,0):'—';$('daily-coverage').textContent=r?String(coverage.ok??'—')+' / '+String(coverage.inScope??coverage.requested??'—'):'—';$('daily-uncovered').textContent=r?String(missing):'—';
    let text='',kind='neutral',badge='待读取';
    if(daily.error){text='清单读取失败：'+daily.error+'。'+(r?'仍展示上次读取的 '+r.targetDate+' 报告，不代表清单已更新。':'当前没有可展示的报告；可点击“刷新清单”重试。');kind='error';badge='读取失败';}
    else if(daily.loading){text=r?'正在检查已有报告，暂保留上次清单…':'正在读取已有扫描报告…';badge='读取中';}
    else if(!r){text=({missing:'还没有正式扫描报告。刷新只读取报告，不会启动扫描。',invalid:'扫描报告未通过校验，暂不展示候选；需先修复或重新生成正式报告。',calendar_unavailable:'交易日历或时间判定不可用，不能确认哪一天是最新清单。'})[status]||'没有可展示的正式报告。';kind=status==='missing'?'neutral':'warning';badge=status==='missing'?'尚无报告':'暂不可用';}
    else if(status==='outdated'){text='旧报告：确认日 '+r.targetDate+'，当前目标日 '+(clock.status?.targetDate||p.expectedTargetDate)+'。以下是历史清单，不是当前目标日的新 B 点。';kind='warning';badge='旧报告';}
    else if(status==='calendar_unavailable'||status==='invalid'){text='无法核验当前目标日，以下仅供复核已保存报告，不能称作最新清单。';kind='warning';badge='日期待核验';}
    else{text='确认日 '+r.targetDate+' 的新 B 点报告已读取。';badge='当前目标日';kind='ready';}
    if(partial){text+=' 部分覆盖：仅 '+coverage.ok+' / '+(coverage.inScope??coverage.requested)+' 只完成分析，'+missing+' 只未覆盖；不能把未覆盖当作无信号。';if(kind!=='error')kind='warning';badge+=' · 部分覆盖';}
    $('daily-status').textContent=text;$('daily-status').className='daily-status '+kind;$('daily-state-badge').textContent=badge;$('daily-state-badge').className='daily-badge '+kind;
    const progress=p?.progress,reasons=p?.summary?.uncoveredReasons||[];
    $('daily-progress').textContent=(progress?.status?'扫描记录：'+(progress.status==='running'?'运行中':progress.status==='finished'?'已结束':progress.status)+' '+(progress.processed??'—')+' / '+(progress.requested??'—')+' · '+(progress.targetDate||'日期未知')+(progress.status==='finished'?'':' · '+(progress.freshness==='recent'?'近期更新':progress.freshness==='stale'?(progress.status==='running'?'记录已久，不能据此确认进程仍在运行':'历史状态记录'):'新鲜度未知'))+'。 ':'')+(reasons.length?'未覆盖原因：'+reasons.map(x=>x.reason+' '+x.count+'只').join('；'):'');
    let empty=daily.loading&&!r?'清单载入中…':daily.error&&!r?'读取失败，暂无可显示的清单。':!r?'尚无可展示的正式清单。':!rows.length?'本次成功分析范围内没有新 B 点'+(missing?'；仍有未覆盖股票。':'。'):'当前筛选没有匹配股票，请调整类型、搜索词或异常标记筛选。';
    $('daily-rows').innerHTML=page.visible.length?page.visible.map(c=>'<tr><td><strong>'+esc(c.name)+'</strong><small>'+esc(c.code)+'</small></td><td>'+c.signals.map(s=>'<span class="daily-signal-chip">'+esc(s.type)+'</span>').join(' ')+'</td><td class="num">'+fmt(c.close,2)+'</td><td>'+c.signals.map(s=>'<span class="daily-fact">'+esc(s.type)+' · '+fmt(s.stop,2)+'</span>').join('')+'</td><td>'+c.signals.map(s=>'<span class="daily-fact">'+esc(s.type)+' · '+(Number.isFinite(s.distanceToStopPct)?fmt(s.distanceToStopPct,2)+'%':'—')+'</span>').join('')+'</td><td class="daily-flags">'+esc(c.flags.join('；')||'—')+'</td><td><button class="button button-quiet daily-open" data-daily-code="'+esc(c.code)+'" '+(daily.snapshotJobId!==null?'disabled':'')+'>查看K线</button>'+watchButton(c.symbol||(/^[69]/.test(c.code)?c.code+'.SS':c.code+'.SZ'),c.name)+'</td></tr>').join(''):'<tr><td colspan="7" class="table-empty">'+esc(empty)+'</td></tr>';
    $('daily-page-info').textContent=r?'筛选 '+page.rows.length+' / '+rows.length+' 只 · 第 '+daily.page+' / '+page.pages+' 页 · 每页 '+daily.pageSize+' 只':'暂无分页';
    $('daily-prev').disabled=!r||daily.page<=1;$('daily-next').disabled=!r||daily.page>=page.pages;$('daily-export').disabled=!r||!page.rows.length;
    $('daily-refresh').disabled=daily.loading;$('daily-refresh').textContent=daily.loading?'正在读取…':'刷新清单';
  }
  async function loadDailyList(){
    if(daily.loading||document.hidden)return;
    daily.loading=true;daily.error='';renderDailyList();const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),15000);
    try{
      const response=await fetch('/api/daily-signals',{cache:'no-store',signal:controller.signal}),payload=await response.json();
      if(!response.ok)throw new Error(payload.error||'服务返回 '+response.status);
      if(payload.schema!=='chanxu-daily-signals-v1'||!['current','outdated','missing','invalid','calendar_unavailable'].includes(payload.reportState)||payload.report&&(!Array.isArray(payload.report.candidates)||!payload.report.reportKey||!payload.report.reportSha256))throw new Error('清单数据格式不正确');
      if(payload.report&&(!payload.report.coverage||!payload.report.candidates.every(c=>c&&/^\d{6}$/.test(c.code)&&typeof c.name==='string'&&Number.isFinite(c.close)&&Array.isArray(c.flags)&&c.flags.every(f=>typeof f==='string')&&Array.isArray(c.signals)&&c.signals.every(s=>s&&['B1','B2','B3'].includes(s.type)&&s.confirmationDate===payload.report.targetDate))))throw new Error('清单候选记录格式不正确');
      if(['current','outdated'].includes(payload.reportState)&&!payload.report)throw new Error('清单状态与报告内容不一致');
      const previous=daily.payload?.report?.reportSha256;daily.payload=payload;if(previous!==payload.report?.reportSha256)daily.page=1;
      if(payload.marketStatus){const previousTarget=clock.status?.targetDate||'',previousDay=clock.status?cnParts(clock.serverEpoch).day:'';if(acceptMarketStatus(payload.marketStatus,true))maybeAutoRefresh('daily-report',previousTarget,previousDay);renderMarketClock();}
    }catch(error){daily.error=error.name==='AbortError'?'请求超时':error.message==='Failed to fetch'?'无法连接本机数据服务':error.message;}
    finally{clearTimeout(timer);daily.loading=false;renderDailyList();}
  }
  function matchDailySignal(analysis,bars,wanted){
    const same=(a,b)=>Number.isFinite(a)&&Number.isFinite(b)?Math.abs(a-b)<=1e-7*Math.max(1,Math.abs(a),Math.abs(b)):a==null&&b==null;
    return analysis.signals.find(s=>s.type===wanted.type&&date(bars[s.index]?.time)===wanted.confirmationDate&&same(s.price,wanted.price)&&same(s.stop,wanted.stop)&&(!wanted.pivotDate||date(bars[s.pivotIndex]?.time)===wanted.pivotDate)&&(!wanted.center||s.center&&same(s.center.ZD,wanted.center.ZD)&&same(s.center.ZG,wanted.center.ZG)));
  }
  async function openDailySnapshot(code){
    const report=daily.payload?.report,candidate=dailyCandidates(report).find(c=>c.code===code);if(!report||!candidate)return;
    const wanted=candidate.signals.find(s=>s.type===daily.type)||candidate.signals[0];if(!wanted)return;
    const job=beginRequest('正在打开 '+report.targetDate+' 清单的 '+candidate.name+' 冻结日K…');daily.snapshotJobId=job.id;renderDailyList();
    try{
      const response=await fetch('/api/daily-signals/snapshot?'+new URLSearchParams({report:report.reportKey,sha:report.reportSha256,code}),{cache:'no-store',signal:job.signal}),payload=await response.json().catch(()=>({}));if(job.id!==state.requestId)return;
      if(!response.ok)throw new Error(payload.error||'快照服务返回 '+response.status);
      if(payload.engineCompatible!==true||payload.candidate?.code!==code||payload.report?.reportKey!==report.reportKey||payload.report?.reportSha256!==report.reportSha256||payload.report?.targetDate!==report.targetDate)throw new Error('报告版本或算法校验不一致，拒绝替换当前图表');
      if(!Array.isArray(payload.bars)||payload.bars.length<5||payload.bars.at(-1).time!==report.targetDate)throw new Error('冻结日K的截止日与报告不一致');
      const verified=ChanEngine.analyze(payload.bars),matched=matchDailySignal(verified,payload.bars,wanted);if(!matched)throw new Error('当前算法未复现所选清单信号，未替换当前图表');
      const meta={...(payload.meta||{}),reportKey:report.reportKey,reportSha256:report.reportSha256,reportTargetDate:report.targetDate,reportGeneratedAt:report.generatedAt};
      applyDataset(payload.bars,{kind:'snapshot',code:candidate.symbol||meta.symbol||code,name:candidate.name,label:report.targetDate+' 清单冻结日K · 未复权',currency:meta.currency||'CNY',meta});
      const selected=matchDailySignal(state.analysis,state.bars,wanted);if(selected)locateSignal(selected.id);
      $('chart-date').value=report.targetDate;requestStatus('已打开 '+report.targetDate+' 清单冻结输入：'+candidate.name+' · '+wanted.type+'，按确认日定位。未拉取最新行情；旧回测已失效，请重新运行。','success');savePrefs();
    }catch(error){if(job.id===state.requestId)requestStatus('清单K线打开失败：'+(error.name==='AbortError'?'请求已取消':error.message)+'。原图表数据保留。','error');}
    finally{if(daily.snapshotJobId===job.id)daily.snapshotJobId=null;finishRequest(job.id);renderDailyList();}
  }
  function csvSafeText(value){const text=String(value??'');return /^[\s\uFEFF]*[=+\-@]/.test(text)||/^[\t\r\n]/.test(text)?"'"+text:text;}
  function loadInitialOnline(saved,initialRequestId){if(state.requestId===initialRequestId&&state.source.kind==='sample'&&!clock.requestActive)fetchOnline(saved);}
  function dailyCSV(){
    const r=daily.payload?.report;if(!r)return '';
    const rows=filteredDailyCandidates().map(c=>({reportDate:r.targetDate,reportState:dailyReportState()+(r.status==='partial'?'/partial':''),code:"'"+c.code,name:csvSafeText(c.name),types:csvSafeText(c.signals.map(s=>s.type).join('/')),close:c.close,stops:csvSafeText(c.signals.map(s=>s.type+':'+s.stop).join(' / ')),distancePct:csvSafeText(c.signals.map(s=>s.type+':'+s.distanceToStopPct).join(' / ')),flags:csvSafeText(c.flags.join('；')),reportSha256:r.reportSha256}));
    return toCSV(rows,['reportDate','reportState','code','name','types','close','stops','distancePct','flags','reportSha256']);
  }
  function precisionFor(bars,source){
    let digits=/^5\d{5}(\.SS)?$/.test(source.code||'')?3:2;
    bars.forEach(b=>['open','high','low','close'].forEach(k=>{for(let n=0;n<=6;n++){if(Math.abs(b[k]-Math.round(b[k]*10**n)/10**n)<1e-9){digits=Math.max(digits,n);break;}}}));
    return Math.min(6,digits);
  }
  function dataDigest(bars){let n=2166136261;const s=JSON.stringify(bars);for(let i=0;i<s.length;i++)n=Math.imul(n^s.charCodeAt(i),16777619);return 'fnv1a-'+(n>>>0).toString(16).padStart(8,'0');}
  function setSourceText(){
    const {source,bars}=state,meta=source.meta||{},label=source.label+' · 截止 '+date(bars.at(-1).time)+(meta.stale?' · 过期缓存':'');
    $('data-state').textContent=label;$('source-status').textContent=label;$('rules-data-note').textContent=label+'。仅作研究，不代表实盘报价。';
    const fetched=meta.fetchedAt?'抓取 '+new Date(meta.fetchedAt).toLocaleString('zh-CN',{hour12:false,timeZone:'Asia/Shanghai'})+' 北京时间':'抓取时间：本地数据';
    const completeness=meta.completenessNote||(source.kind==='csv'?'本地CSV完成度需自行核实':source.kind==='sample'?'合成示例含非交易日，不代表市场日历':'研究快照的行情完成度需自行核实');
    $('data-freshness').textContent=fetched+(Number.isFinite(meta.cacheAgeSeconds)?' · 缓存约 '+Math.floor(meta.cacheAgeSeconds/60)+' 分钟':'')+' · '+completeness;
    $('currency-unit').textContent=source.currency||(source.kind==='sample'?'模拟单位':'报价单位');
    $('market-assumptions').textContent='金额按'+(source.currency||'当前报价单位')+'计量，不做换汇。交易单位与 T+1 是手动研究假设，切换市场不会自动更改，请自行核对。';
  }
  function clearSelection(){state.selectedSignal=null;$('signal-select').value='';$('signal-detail').className='detail-empty';$('signal-detail').innerHTML='<strong>等待选择当前数据的信号</strong><p>点击标记或使用信号清单；同日多个信号可分别选择。</p>';}
  function applyDataset(bars,source,restore){
    if(bars.length>5000)throw new Error('当前最多支持5000根日K，请缩短CSV范围后重试');
    const normalized=bars.map((b,i)=>({...b,time:ChanEngine.normalizeTradingDay(b.time,i+1)})),verified=ChanEngine.analyze(normalized);
    state.bars=normalized;state.analysis=verified;state.source=source;state.datasetId=(source.code||source.name)+':'+dataDigest(normalized);state.precision=precisionFor(normalized,source);state.end=null;state.runId='';
    clearSelection();resetBacktest();setSourceText();renderAll();
    if(restore?.datasetId===state.datasetId){state.end=Number.isInteger(restore.end)?Math.min(normalized.length,Math.max(5,restore.end)):null;const s=verified.signals.find(s=>s.id===restore.selectedSignalId);if(s)selectSignal(s,false);renderChart();}
  }
  function renderAll(){renderSummary();renderChart();renderSnapshot();renderSignalList();renderMarketClock();}
  function renderSummary(){
    const b=state.bars,last=b.at(-1),delta=last.close/b.at(-2).close-1,p=state.analysis.pens.at(-1),recent=state.analysis.signals.at(-1);
    $('latest-price').textContent=price(last.close);$('price-change').textContent=pct(delta)+' 较前收';$('price-change').className=delta>=0?'positive':'negative';
    $('latest-date').textContent=date(last.time);$('date-range').textContent=date(b[0].time)+' 至 '+date(last.time);$('instrument').textContent=(state.source.name||state.source.code)+' · '+(state.source.code||'CSV');$('chart-instrument').textContent=$('instrument').textContent;renderWatchlist();
    $('trend-state').textContent=p?(p.direction==='up'?'上行笔':'下行笔'):'结构形成中';$('latest-signal').textContent=recent?signalLabel(recent.type):'暂无确认信号';$('signal-caption').textContent=recent?date(b[recent.index].time)+' · '+price(recent.price):'仅显示已确认的实验代理';
    $('chart-counts').textContent=b.length+' 根日K · '+state.analysis.signals.length+' 个实验信号';$('chart-date').min=date(b[0].time);$('chart-date').max=date(last.time);
  }
  function svgEl(name,attrs={},text){const n=document.createElementNS('http://www.w3.org/2000/svg',name);Object.entries(attrs).forEach(([k,v])=>n.setAttribute(k,v));if(text!=null)n.textContent=text;return n;}
  function renderChart(){
    const svg=$('price-chart'),all=state.bars,end=state.end||all.length,requested=state.window==='all'?all.length:Number(state.window),start=Math.max(0,end-requested),bars=all.slice(start,end);
    const W=Math.max(340,svg.parentElement.clientWidth||900),H=450,pad={l:18,r:76,t:35,b:38},plotW=W-pad.l-pad.r,plotH=H-pad.t-pad.b;
    const lo=Math.min(...bars.map(b=>b.low)),hi=Math.max(...bars.map(b=>b.high)),span=Math.max(hi-lo,1e-6),y=p=>pad.t+(hi-p)/span*plotH,x=i=>pad.l+(i+.5)*plotW/bars.length,step=plotW/bars.length;
    svg.replaceChildren();svg.setAttribute('viewBox','0 0 '+W+' '+H);svg.setAttribute('preserveAspectRatio','none');
    $('chart-subtitle').textContent=state.source.label+' · 显示 '+date(bars[0].time)+' 至 '+date(bars.at(-1).time)+' · '+bars.length+' 根日K';
    for(let t=0;t<=4;t++){const val=hi-span*t/4,yy=y(val);svg.append(svgEl('line',{x1:pad.l,x2:W-pad.r,y1:yy,y2:yy,stroke:colors.grid,'pointer-events':'none'}));svg.append(svgEl('text',{x:W-pad.r+7,y:yy+4,'font-size':11},price(val)));}
    for(let t=0;t<=3;t++){const i=Math.min(bars.length-1,Math.round((bars.length-1)*t/3));svg.append(svgEl('text',{x:Math.max(39,x(i)),y:H-9,'text-anchor':'middle','font-size':10},date(bars[i].time)));}
    bars.forEach((b,i)=>{const color=b.close>=b.open?colors.up:colors.down,cw=Math.max(1,Math.min(9,step*.58));svg.append(svgEl('line',{x1:x(i),x2:x(i),y1:y(b.high),y2:y(b.low),stroke:color,'pointer-events':'none'}));svg.append(svgEl('rect',{x:x(i)-cw/2,y:Math.min(y(b.open),y(b.close)),width:cw,height:Math.max(1,Math.abs(y(b.open)-y(b.close))),fill:b.close>=b.open?'rgba(238,141,128,.22)':color,stroke:color,'pointer-events':'none'}));const hit=svgEl('rect',{x:pad.l+i*step,y:pad.t,width:step,height:plotH,fill:'transparent'});hit.addEventListener('pointerenter',e=>showTooltip(e,start+i));hit.addEventListener('pointerleave',()=>$('chart-tooltip').hidden=true);svg.append(hit);});
    const visible=i=>i>=start&&i<end;
    if(state.layers.centers)state.analysis.centers.filter(c=>c.endIndex>=start&&c.startIndex<end).forEach(c=>{const left=Math.max(start,c.startIndex),right=Math.min(end-1,c.endIndex);svg.append(svgEl('rect',{x:x(left-start),y:y(c.ZG),width:Math.max(2,x(right-start)-x(left-start)),height:Math.max(3,y(c.ZD)-y(c.ZG)),fill:'rgba(226,171,88,.10)',stroke:'rgba(226,171,88,.38)','stroke-dasharray':'4 3','pointer-events':'none'}));});
    for(const [layer,items] of [['pens',state.analysis.pens],['segments',state.analysis.segments]])if(state.layers[layer])items.forEach(p=>{if(visible(p.start.index)&&visible(p.end.index))svg.append(svgEl('line',{x1:x(p.start.index-start),y1:y(p.start.price),x2:x(p.end.index-start),y2:y(p.end.price),stroke:colors.amber,'stroke-width':layer==='pens'?1.3:2,'stroke-dasharray':layer==='pens'?'':'7 5',opacity:.7,'pointer-events':'none'}));});
    if(state.layers.fractals)state.analysis.fractals.filter(f=>visible(f.index)).forEach(f=>{const xx=x(f.index-start),yy=y(f.price);svg.append(svgEl('path',{d:f.type==='top'?'M'+(xx-4)+','+(yy-4)+' L'+(xx+4)+','+(yy-4)+' L'+xx+','+(yy+3)+' Z':'M'+(xx-4)+','+(yy+4)+' L'+(xx+4)+','+(yy+4)+' L'+xx+','+(yy-3)+' Z',fill:colors.amber,'pointer-events':'none'}));});
    const lanes=new Map();
    if(state.layers.signals)state.analysis.signals.filter(s=>visible(s.index)).forEach(s=>{const buy=s.type.startsWith('B'),key=s.index+':'+buy,lane=lanes.get(key)||0;lanes.set(key,lane+1);const xx=x(s.index-start),base=buy?Math.min(y(s.price)+19,H-pad.b-14):Math.max(y(s.price)-19,pad.t+14),yy=base+(buy?-1:1)*lane*27;
      const g=svgEl('g',{class:'chart-signal',tabindex:0,role:'button','data-signal-id':s.id,'data-lane':lane,'aria-pressed':String(state.selectedSignal?.id===s.id),'aria-label':signalLabel(s.type)+' '+date(all[s.index].time)+' '+price(s.price)});
      g.append(svgEl('line',{x1:xx,x2:xx,y1:y(s.price),y2:yy,stroke:buy?colors.up:colors.down,opacity:.4}));g.append(svgEl('rect',{x:xx-20,y:yy-11,width:40,height:22,rx:7,fill:'#252e27',stroke:buy?colors.up:colors.down,'stroke-width':state.selectedSignal?.id===s.id?3:1}));g.append(svgEl('text',{x:xx,y:yy+4,'text-anchor':'middle',fill:buy?colors.up:colors.down,'font-size':11},s.type));g.addEventListener('click',()=>selectSignal(s));g.addEventListener('keydown',e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();selectSignal(s);}});svg.append(g);
    });
  }
  function showTooltip(e,i){const b=state.bars[i],el=$('chart-tooltip'),box=$('chart-wrap').getBoundingClientRect();el.hidden=false;el.innerHTML='<b>'+esc(date(b.time))+'</b><span>开 '+price(b.open)+'　高 '+price(b.high)+'</span><span>低 '+price(b.low)+'　收 '+price(b.close)+'</span><span>量 '+fmt(b.volume,0)+'</span>';el.style.left=Math.max(0,Math.min(e.clientX-box.left+10,box.width-205))+'px';el.style.top=Math.max(8,e.clientY-box.top-50)+'px';}
  function option(value,text){const n=document.createElement('option');n.value=value;n.textContent=text;return n;}
  function renderSignalList(){const select=$('signal-select');select.replaceChildren(option('','选择信号 · 共'+state.analysis.signals.length+'项'));state.analysis.signals.slice().reverse().forEach(s=>select.append(option(s.id,date(state.bars[s.index].time)+' '+signalLabel(s.type)+' '+price(s.price))));select.value=state.selectedSignal?.id||'';}
  function selectSignal(s,persist=true){
    if(!state.analysis.signals.some(v=>v.id===s.id))return;state.selectedSignal=s;$('signal-select').value=s.id;const el=$('signal-detail');el.className='signal-detail';
    el.innerHTML='<div class="selected-type">'+esc(signalLabel(s.type))+'<span>实验代理</span></div><p class="signal-dataset">'+esc(state.source.name)+' · '+esc(state.source.code||'CSV')+'</p><div class="evidence-row"><span>确认时间</span><b>'+esc(date(state.bars[s.index].time))+'</b></div><div class="evidence-row"><span>转折位置</span><b>'+esc(date(state.bars[s.pivotIndex].time))+'</b></div><div class="evidence-row"><span>参考价格</span><b>'+price(s.price)+'</b></div><div class="evidence-row"><span>失效参考</span><b>'+(Number.isFinite(s.stop)?price(s.stop):'未定义')+'</b></div>'+(s.center?'<div class="evidence-row"><span>冻结中枢</span><b>'+price(s.center.ZD)+'–'+price(s.center.ZG)+'</b></div>':'')+'<p class="signal-reason">'+esc(s.reason)+'</p><p class="signal-warning">标记是确认日，参考价来自转折位置；历史结构全样本重绘，信号依据按当时冻结。</p>';renderChart();if(persist)savePrefs();
  }
  function locateSignal(id){const s=state.analysis.signals.find(v=>v.id===id);if(!s)return;state.window=120;state.end=Math.min(state.bars.length,Math.max(120,s.index+12));selectSignal(s);syncWindowButtons();navigate('chart');}
  function renderSnapshot(){const a=state.analysis;$('snap-fractals').textContent=a.fractals.length;$('snap-pens').textContent=a.pens.length;$('snap-segments').textContent=a.segments.length+' · 代理';$('snap-centers').textContent=a.centers.length?a.centers.length+' 个 · 最近 '+price(a.centers.at(-1).ZD)+'–'+price(a.centers.at(-1).ZG):'暂无有效重叠';}
  function resetBacktest(){state.backtest=null;state.runId='';['return','dd','win','pf','count','fees'].forEach(k=>$('metric-'+k).textContent='—');$('summary-dd').textContent='待回测';$('summary-trades').textContent='运行回测后展示';$('equity-empty').hidden=false;$('equity-chart').replaceChildren();$('equity-caption').textContent='运行回测后显示净值曲线';$('equity-readout').textContent='';$('trade-rows').innerHTML='<tr><td colspan="8" class="table-empty">当前无有效回测，请运行回测。</td></tr>';$('trade-count-label').textContent='等待回测';$('export-trades').disabled=true;$('account-breakdown').textContent='回测后展示现金、持仓与盈亏分解';}
  function readOptions(showErrors=true){
    const values={},problems=[],names={initialCapital:'初始资金',positionPct:'单次仓位',riskPct:'单笔风险预算',lotSize:'交易单位',feeBps:'佣金',slippageBps:'滑点'};
    Object.entries(FIELDS).forEach(([key,id])=>{const input=$(id+'-input'),n=String(input.value).trim()===''?NaN:Number(input.value);values[key]=n;const valid=Number.isFinite(n)&&(key==='initialCapital'?n>0:key==='lotSize'?Number.isInteger(n)&&n>=1:['positionPct','riskPct'].includes(key)?n>0&&n<=100:n>=0&&n<10000);if(!valid)problems.push({id,message:names[key]+(key==='initialCapital'?'需大于0':key==='lotSize'?'需为大于等于1的整数':['positionPct','riskPct'].includes(key)?'需大于0且不超过100%':'需大于等于0且小于10000 bp')});if(showErrors){$(id+'-error').textContent='';input.setAttribute('aria-invalid','false');}});
    values.tPlusOne=$('t1-input').checked;
    if(problems.length){if(showErrors){problems.forEach(p=>{$(p.id+'-error').textContent=p.message;$(p.id+'-input').setAttribute('aria-invalid','true');});$(problems[0].id+'-input').focus();$('parameter-status').textContent='请修正标出的参数后再运行。';}return null;}return values;
  }
  function setOptions(o){Object.entries(FIELDS).forEach(([key,id])=>{if(Number.isFinite(o?.[key]))$(id+'-input').value=String(o[key]);});if(typeof o?.tPlusOne==='boolean')$('t1-input').checked=o.tPlusOne;}
  function runBacktest(){const options=readOptions();if(!options)return;try{const r=ChanEngine.backtest(state.bars,options);state.backtest=r;state.runId=new Date().toISOString()+'-'+state.datasetId;renderBacktest(r);savePrefs();$('parameter-status').textContent='回测完成；更改参数后需重新运行。';}catch(e){const id=FIELDS[e.field];if(id){$(id+'-error').textContent=e.message;$(id+'-input').focus();}$('parameter-status').textContent='回测失败：'+e.message;toast($('parameter-status').textContent);}}
  function renderBacktest(r){
    const m=r.metrics;$('metric-return').textContent=pct(m.totalReturn);$('metric-return').className=m.totalReturn>=0?'good':'bad';$('metric-dd').textContent=pct(-m.maxDrawdown);$('metric-win').textContent=m.tradeCount?(m.winRate*100).toFixed(1)+'%':'无样本';$('metric-pf').textContent=m.tradeCount?(Number.isFinite(m.profitFactor)?fmt(m.profitFactor):'无亏损样本'):'无样本';$('metric-count').textContent=m.tradeCount;$('metric-fees').textContent=money(m.totalFees);$('summary-dd').textContent=pct(-m.maxDrawdown);$('summary-trades').textContent=m.tradeCount+' 笔已平仓';$('equity-empty').hidden=true;$('equity-caption').textContent='期末净值 '+money(m.finalEquity)+' · '+(r.openPosition?'仍持仓，按末日收盘估值':'期末空仓');
    const p=r.openPosition,market=p?p.qty*state.bars.at(-1).close:0,realized=r.trades.filter(t=>t.side==='SELL').reduce((n,t)=>n+t.pnl,0),unrealized=p?(state.bars.at(-1).close-p.entryPrice)*p.qty-p.entryFees:0;
    $('account-breakdown').innerHTML='<span>现金 <b>'+money(m.finalEquity-market)+'</b></span><span>持仓 <b>'+(p?fmt(p.qty,0)+' 份/股 · 成本 '+price(p.entryPrice):'无')+'</b></span><span>已实现净盈亏 <b>'+money(realized)+'</b></span><span>未实现盈亏 <b>'+money(unrealized)+'</b><small>已扣入场费，未扣未来退出费</small></span>';
    renderEquity(r.equity);renderTrades(r.trades.filter(t=>t.side==='SELL'));$('trade-count-label').textContent=m.tradeCount+' 笔已平仓交易 · 点击类型查看入场证据';$('export-trades').disabled=false;
  }
  function renderEquity(points){
    const svg=$('equity-chart'),W=Math.max(340,svg.parentElement.clientWidth||800),H=220,pad={l:85,r:20,t:16,b:35},vals=points.map(p=>p.value),lo=Math.min(...vals),hi=Math.max(...vals),span=Math.max(hi-lo,1),x=i=>pad.l+i/Math.max(1,vals.length-1)*(W-pad.l-pad.r),y=v=>H-pad.b-(v-lo)/span*(H-pad.t-pad.b);
    svg.replaceChildren();svg.setAttribute('viewBox','0 0 '+W+' '+H);
    for(let t=0;t<=3;t++){const v=lo+span*t/3,yy=y(v);svg.append(svgEl('line',{x1:pad.l,x2:W-pad.r,y1:yy,y2:yy,stroke:colors.grid}));svg.append(svgEl('text',{x:pad.l-8,y:yy+4,'text-anchor':'end'},fmt(v,0)));}
    for(let t=0;t<=2;t++){const i=Math.round((points.length-1)*t/2);svg.append(svgEl('text',{x:x(i),y:H-9,'text-anchor':'middle'},date(points[i].time)));}
    svg.append(svgEl('path',{d:vals.map((v,i)=>(i?'L':'M')+x(i)+','+y(v)).join(' '),fill:'none',stroke:colors.amber,'stroke-width':2}));
    points.forEach((p,i)=>{const hit=svgEl('rect',{x:x(i)-Math.max(2,(W-pad.l-pad.r)/points.length/2),y:pad.t,width:Math.max(4,(W-pad.l-pad.r)/points.length),height:H-pad.t-pad.b,fill:'transparent'});hit.append(svgEl('title',{},date(p.time)+' · '+money(p.value)));hit.addEventListener('pointerenter',()=>{$('equity-readout').textContent=date(p.time)+' · 净值 '+money(p.value);});svg.append(hit);});
    $('equity-readout').textContent='末日 '+date(points.at(-1).time)+' · '+money(points.at(-1).value)+'；悬停曲线读取日期和净值';
  }
  function renderTrades(trades){
    if(!trades.length){$('trade-rows').innerHTML='<tr><td colspan="8" class="table-empty">没有已平仓交易；查看持仓或调整研究范围后重试。</td></tr>';return;}
    const buys=new Map(state.backtest.trades.filter(t=>t.side==='BUY').map(t=>[t.index,t.signalId]));
    $('trade-rows').innerHTML=trades.slice().reverse().map((t,i)=>{const id=buys.get(t.entryIndex),s=state.analysis.signals.find(v=>v.id===id);return '<tr><td>'+(trades.length-i)+'</td><td>'+esc(date(state.bars[t.entryIndex].time))+'</td><td><button class="trade-link" data-signal-id="'+esc(id)+'">'+esc(signalLabel(s?.type||'入场'))+'<small>'+esc(id)+'</small></button></td><td class="num">'+price(t.entryPrice)+'</td><td>'+esc(date(state.bars[t.exitIndex].time))+'</td><td>'+esc(exitReason(t.reason))+'</td><td class="num">'+price(t.exitPrice)+'</td><td class="num '+(t.pnl>=0?'positive':'negative')+'">'+fmt(t.pnl)+'</td></tr>';}).join('');
  }
  function beginRequest(label){state.controller?.abort();state.controller=new AbortController();clock.requestActive=true;const id=++state.requestId;$('fetch-btn').disabled=true;$('fetch-btn').textContent='正在读取…';$('cancel-request').hidden=false;requestStatus(label,'loading');return {id,signal:state.controller.signal};}
  function finishRequest(id){if(id!==state.requestId)return;clock.requestActive=false;$('fetch-btn').disabled=false;$('fetch-btn').textContent='拉取行情';$('cancel-request').hidden=true;renderMarketClock();}
  function cancelRequest(){state.controller?.abort();state.requestId++;clock.requestActive=false;clock.autoPausedUntil=performance.now()+AUTO_RETRY_MS;$('fetch-btn').disabled=false;$('fetch-btn').textContent='拉取行情';$('cancel-request').hidden=true;requestStatus('已取消读取；当前数据保持不变，自动行情重试暂停2分钟。');}
  function directSymbol(raw){if(/^ETF$/i.test(String(raw).trim()))return false;if(/^(?:920\d{3}|[48]\d{5})(?:\.BJ)?$/i.test(String(raw).trim()))return false;return /^(?:\d{6}(?:\.(?:SS|SZ|BJ))?|\^?[A-Z][A-Z0-9.^=-]{0,24})$/i.test(String(raw).trim());}
  function normalizeSymbol(raw){const v=String(raw).trim();if(!v)throw new Error('请输入代码，或输入中文名搜索候选');if(!directSymbol(v))throw new Error('请先从名称搜索结果中选择证券');if(/\.BJ$/i.test(v))throw new Error('当前行情源暂不支持北交所；可搜索查看证券信息');return v.toUpperCase();}
  function instrumentKind(item){return ({stock:'股票',etf:'ETF',index:'指数',foreign:'海外证券'})[item.kind]||'证券';}
  function instrumentExchange(item){return ({SH:'上海',SZ:'深圳',BJ:'北交所',HK:'香港',US:'美国'})[item.exchange]||item.exchange||(/\.SS$/.test(item.symbol)?'上海':/\.SZ$/.test(item.symbol)?'深圳':/\.BJ$/.test(item.symbol)?'北交所':'市场未标注');}
  function symbolCoverageText(meta){
    if(!meta)return '搜索范围不等于每日选股范围，不套用每日清单的板块/ST排除。';
    const coverage=meta.coverage;if(!coverage)return '当前服务可用目录 · 日期 '+(meta.snapshotDate||'未标注')+'；覆盖统计未提供，不据此宣称全量。搜索范围不等于每日选股范围。';
    const statuses={partial:'部分目录',stale:'旧目录',missing:'尚未取得',invalid:'目录无效',unavailable:'暂不可用',error:'目录读取失败',refresh_failed:'目录刷新失败',unknown:'待核验'};
    const part=(value,label)=>value?label+' '+(value.count??'—')+' 项（'+(statuses[value.status]||(value.complete?'目录已核验':'目录未齐'))+(value.complete&&value.status==='stale'?'，缓存完整性已核验':'')+'，'+(value.status==='stale'?'保留原日期 ':'日期 ')+(value.snapshotDate||'未标注')+'）'+(typeof value.message==='string'&&value.message?'；'+value.message:''):label+'覆盖未提供';
    return '可搜索 '+(coverage.total??'—')+' 项 · '+part(coverage.stocks,'股票')+' · '+part(coverage.etfs,'ETF')+'。'+(coverage.note||'')+' 搜索范围不等于每日选股范围。';
  }
  function renderSymbolSearch(){
    const input=$('symbol-input'),panel=$('symbol-search-panel');if(!panel)return;
    panel.hidden=!symbolSearch.open;input.setAttribute('aria-expanded',String(symbolSearch.open));input.setAttribute('aria-activedescendant',symbolSearch.open&&symbolSearch.activeIndex>=0?'symbol-option-'+symbolSearch.activeIndex:'');
    const count=symbolSearch.meta?.totalMatches??symbolSearch.results.length;
    $('symbol-search-status').textContent=symbolSearch.loading?'正在搜索证券目录…':symbolSearch.error?'搜索失败：'+symbolSearch.error:symbolSearch.composing?'完成输入后搜索':!symbolSearch.query?'输入中文名、代码或名称片段':symbolSearch.results.length?'找到 '+count+' 项'+(symbolSearch.meta?.truncated?'，仅展示前 '+symbolSearch.results.length+' 项，请缩小关键词':'')+'。请明确选择证券后读取行情。':'没有匹配结果。请尝试代码、简称片段，或检查下方目录覆盖情况。';
    $('symbol-results').innerHTML=symbolSearch.results.map((item,index)=>'<div id="symbol-option-'+index+'" class="symbol-option'+(index===symbolSearch.activeIndex?' active':'')+(item.historySupported===false?' unsupported':'')+'" role="option" aria-selected="'+(index===symbolSearch.activeIndex)+'" aria-disabled="'+(item.historySupported===false)+'" data-symbol-choice="'+index+'"><div><strong>'+esc(item.name)+'</strong><span>'+esc(item.code)+' · '+esc(item.symbol)+'</span></div><small>'+esc(instrumentExchange(item))+' · '+esc(instrumentKind(item))+'</small>'+(item.historySupported===false?'<p>'+esc(item.reason||'当前行情源暂不支持此证券')+'</p>':'')+'</div>').join('');
    $('symbol-search-coverage').textContent=symbolCoverageText(symbolSearch.meta);
  }
  function closeSymbolSearch(){clearTimeout(symbolSearch.timer);symbolSearch.controller?.abort();symbolSearch.id++;symbolSearch.loading=false;symbolSearch.open=false;symbolSearch.activeIndex=-1;renderSymbolSearch();}
  function invalidateSymbolHistory(){const wasActive=clock.requestActive;state.controller?.abort();state.requestId++;symbolSearch.inputRevision++;symbolSearch.editing=true;clock.requestActive=false;clock.autoPausedUntil=performance.now()+AUTO_RETRY_MS;$('fetch-btn').disabled=false;$('fetch-btn').textContent='拉取行情';$('cancel-request').hidden=true;if(wasActive)requestStatus('输入已更改，旧行情读取已取消；原图表保留，尚未读取新证券。');}
  function queueSymbolSearch(raw){
    invalidateSymbolHistory();clearTimeout(symbolSearch.timer);symbolSearch.controller?.abort();symbolSearch.id++;symbolSearch.query=String(raw).trim();symbolSearch.results=[];symbolSearch.meta=null;symbolSearch.error='';symbolSearch.activeIndex=-1;symbolSearch.open=!!symbolSearch.query;symbolSearch.loading=!!symbolSearch.query&&!symbolSearch.composing;renderSymbolSearch();
    if(symbolSearch.query&&!symbolSearch.composing)symbolSearch.timer=setTimeout(()=>searchSymbols(symbolSearch.query),300);
  }
  async function searchSymbols(raw){
    clearTimeout(symbolSearch.timer);symbolSearch.controller?.abort();const id=++symbolSearch.id;symbolSearch.query=String(raw).trim();symbolSearch.results=[];symbolSearch.meta=null;symbolSearch.error='';symbolSearch.activeIndex=-1;symbolSearch.open=!!symbolSearch.query;if(!symbolSearch.query){symbolSearch.loading=false;renderSymbolSearch();return false;}
    const inputRevision=symbolSearch.inputRevision,controller=new AbortController();symbolSearch.controller=controller;symbolSearch.loading=true;renderSymbolSearch();const timeout=setTimeout(()=>controller.abort(),12000);
    try{const response=await fetch('/api/instruments?'+new URLSearchParams({query:symbolSearch.query}),{cache:'no-store',signal:controller.signal}),payload=await response.json();if(id!==symbolSearch.id||inputRevision!==symbolSearch.inputRevision)return false;if(!response.ok)throw new Error(payload.error||'证券目录服务不可用');if(!Array.isArray(payload.items)||!payload.items.every(item=>item&&typeof item.symbol==='string'&&typeof item.code==='string'&&typeof item.name==='string'))throw new Error('证券候选格式不正确');symbolSearch.results=payload.items;symbolSearch.meta=payload;return true;}
    catch(error){if(id===symbolSearch.id&&inputRevision===symbolSearch.inputRevision)symbolSearch.error=error.name==='AbortError'?'请求超时或已取消':error.message==='Failed to fetch'?'无法连接本机证券目录':error.message;return false;}
    finally{clearTimeout(timeout);if(id===symbolSearch.id&&inputRevision===symbolSearch.inputRevision){symbolSearch.loading=false;symbolSearch.controller=null;renderSymbolSearch();}}
  }
  async function chooseSymbolResult(index){
    const item=symbolSearch.results[index];if(!symbolSearch.open||!item)return false;if(item.historySupported===false){requestStatus(item.reason||'当前行情源暂不支持此证券','warning');return false;}
    invalidateSymbolHistory();closeSymbolSearch();symbolSearch.editing=false;$('symbol-input').value=item.symbol;const loaded=await fetchOnline();if(loaded)navigate('chart');return loaded;
  }
  function symbolKeydown(event){
    if(event.isComposing||symbolSearch.composing||event.keyCode===229)return;
    if(event.key==='Escape'){event.preventDefault();closeSymbolSearch();return;}
    if((event.key==='ArrowDown'||event.key==='ArrowUp')&&symbolSearch.open&&symbolSearch.results.length){event.preventDefault();const n=symbolSearch.results.length;symbolSearch.activeIndex=event.key==='ArrowDown'?(symbolSearch.activeIndex+1)%n:symbolSearch.activeIndex<0?n-1:(symbolSearch.activeIndex-1+n)%n;renderSymbolSearch();$('symbol-option-'+symbolSearch.activeIndex)?.scrollIntoView({block:'nearest'});return;}
    if(event.key==='Enter'){event.preventDefault();if(symbolSearch.open&&symbolSearch.activeIndex>=0)return chooseSymbolResult(symbolSearch.activeIndex);return fetchOnline();}
  }
  function snapshotSource(raw){
    const text=(v,n)=>typeof v==='string'?v.slice(0,n):'';
    const meta=raw?.meta||{},safeMeta={};
    ['fetchedAt','completenessNote','source','asOf','adjustment','completenessPolicy','reportKey','reportSha256','reportTargetDate','reportGeneratedAt'].forEach(k=>{if(typeof meta[k]==='string')safeMeta[k]=text(meta[k],500);});
    safeMeta.stale=meta.stale===true;if(Number.isFinite(meta.cacheAgeSeconds)&&meta.cacheAgeSeconds>=0)safeMeta.cacheAgeSeconds=meta.cacheAgeSeconds;
    return {kind:'snapshot',code:text(raw?.code,80)||'SNAPSHOT',name:text(raw?.name,160)||'导入研究快照',label:'研究快照 · 日线',currency:typeof raw?.currency==='string'&&/^[A-Z]{3}$/.test(raw.currency)?raw.currency:'',meta:safeMeta};
  }
  async function hashBars(bars,algorithm){
    if(algorithm==='SHA-256'){
      if(!window.crypto?.subtle)throw new Error('当前环境不支持SHA-256校验，请在本机HTTP页面导入快照');
      const digest=await window.crypto.subtle.digest('SHA-256',new TextEncoder().encode(JSON.stringify(bars)));
      return Array.from(new Uint8Array(digest),b=>b.toString(16).padStart(2,'0')).join('');
    }
    if(algorithm==='FNV-1a (non-cryptographic)'||algorithm==='FNV-1a')return dataDigest(bars);
    throw new Error('快照缺少支持的数据校验算法，请重新导出');
  }
  function validSnapshotOptions(o){
    return o&&Object.keys(FIELDS).every(k=>Number.isFinite(o[k]))&&o.initialCapital>0&&o.positionPct>0&&o.positionPct<=100&&o.riskPct>0&&o.riskPct<=100&&Number.isInteger(o.lotSize)&&o.lotSize>=1&&o.feeBps>=0&&o.feeBps<10000&&o.slippageBps>=0&&o.slippageBps<10000&&typeof o.tPlusOne==='boolean';
  }
  async function fetchOnline(restore,options={}){
    if(options.automatic&&(clock.requestActive||symbolSearch.editing||state.source.kind!=='online'))return;
    if(!options.automatic&&$('symbol-input').value.trim()&&!directSymbol($('symbol-input').value)){const query=$('symbol-input').value.trim();invalidateSymbolHistory();const found=await searchSymbols(query);if(found&&symbolSearch.open&&$('symbol-input').value.trim()===query)requestStatus(symbolSearch.results.length?'请选择候选中的证券后再读取；不会自动选择同名股票或 ETF。':'未找到匹配证券，请尝试更短名称或代码，并检查目录覆盖。');return false;}
    let symbol;try{symbol=options.automatic?state.source.code:normalizeSymbol($('symbol-input').value);}catch(e){requestStatus(e.message,'error');$('symbol-input').focus();return;}
    if(!options.automatic){closeSymbolSearch();symbolSearch.editing=false;$('symbol-input').value=symbol;}
    const range=options.automatic?(state.source.meta?.range||$('range-input').value):$('range-input').value;
    clock.lastAutoAttempt=performance.now();const job=beginRequest((options.automatic?'收盘时钟自动更新 ':'正在读取 ')+symbol+'，当前图表数据暂不替换…');
    try{
      const resp=await fetch('/api/history?'+new URLSearchParams({symbol,range,refresh:'1'}),{cache:'no-store',signal:job.signal}),payload=await resp.json().catch(()=>({}));if(job.id!==state.requestId)return;
      if(!resp.ok)throw new Error(payload.error||'行情服务返回'+resp.status);if(!Array.isArray(payload.bars)||payload.bars.length<5)throw new Error('行情源返回的完整日K不足5根');
      const meta=payload.meta||{},label=(meta.source||'在线行情')+' · 日线'+(meta.adjustment?.startsWith('unadjusted')?' · 不复权':'');
      if(meta.marketStatus)acceptMarketStatus(meta.marketStatus,true);
      applyDataset(payload.bars,{kind:'online',code:meta.symbol||symbol,name:preferredName(meta,symbol),label,currency:typeof meta.currency==='string'&&/^[A-Z]{3}$/.test(meta.currency)?meta.currency:'',meta},restore);
      const waiting=isChinaOnline()&&(meta.readiness!=='ready'||(clock.status&&date(state.bars.at(-1).time)!==clock.status.targetDate));
      requestStatus('已读取 '+state.bars.length+' 根 '+state.source.code+' 日K，实际截止 '+date(state.bars.at(-1).time)+(meta.stale?'。正在使用过期缓存，不能作为最新收盘信号。':waiting?'。等待数据源发布 '+(meta.expectedThrough||clock.status?.targetDate||'目标日')+' 完整日K，自动重试中。':'。已按实际完整日K重新计算。'),meta.stale||waiting?'warning':'success');savePrefs();return true;
    }catch(e){if(job.id!==state.requestId)return;requestStatus('读取失败：'+(e.name==='AbortError'?'请求已取消':e.message==='Failed to fetch'?'无法连接本机行情服务，请检查服务是否启动后重试':e.message)+'。当前数据未替换。','error');}finally{finishRequest(job.id);}
  }
  async function importFile(input,snapshot=false){
    const f=input.files?.[0];if(!f)return;const job=beginRequest('正在读取本地'+(snapshot?'研究快照':'CSV')+' '+f.name+'…');
    try{
      if(f.size>5*1024*1024)throw new Error('文件超过5MB，请缩短日K范围后重试');const text=await f.text();if(job.id!==state.requestId)return;let bars,source,options;
      let ruleMismatch=false;
      if(snapshot){const data=JSON.parse(text.replace(/^\uFEFF/,''));if(data.schema!=='chanxu-research-v1'||!Array.isArray(data.bars))throw new Error('不是支持的缠序研究快照');if(!validSnapshotOptions(data.options))throw new Error('快照参数缺失或无效，请重新导出');const expected=await hashBars(data.bars,data.hashAlgorithm);if(job.id!==state.requestId)return;if(typeof data.dataHash!=='string'||data.dataHash!==expected)throw new Error('快照数据校验不一致，文件可能被修改或损坏；校验仅检查完整性，不是身份签名');bars=ChanEngine.parseCSV(toCSV(data.bars,['time','open','high','low','close','volume']));source=snapshotSource(data.source);options=data.options;ruleMismatch=data.ruleVersion!==state.analysis.ruleVersion;}
      else{bars=ChanEngine.parseCSV(text);source={kind:'csv',code:f.name,name:f.name,label:'本地CSV · 日线',currency:'',meta:{}};}
      applyDataset(bars,source);if(options){setOptions(options);requestStatus(ruleMismatch?'快照规则版本与当前版本不同，将按当前规则重新计算；不能宣称复现旧结果。数据校验仅为完整性检查，不是身份签名。':'研究快照已通过数据完整性校验；这不是身份签名。请重新运行回测，未使用文件中的旧结果。',ruleMismatch?'warning':'success');}
      else requestStatus('已载入 '+bars.length+' 根本地日K。CSV不含币种，金额暂以报价单位计量。','success');savePrefs();
    }catch(e){if(job.id===state.requestId)requestStatus('本地文件读取失败：'+e.message+'。当前数据未替换。','error');}finally{input.value='';finishRequest(job.id);}
  }
  function toCSV(rows,keys){return [keys.join(','),...rows.map(r=>keys.map(k=>{const v=String(r[k]??'');return /[\r\n",]/.test(v)?'"'+v.replace(/"/g,'""')+'"':v;}).join(','))].join('\r\n');}
  function download(name,text,type='text/csv;charset=utf-8'){const a=document.createElement('a');a.href=URL.createObjectURL(new Blob([type.startsWith('text/csv')?'\ufeff':'',text],{type}));a.download=name;a.click();setTimeout(()=>URL.revokeObjectURL(a.href),1000);}
  const safeName=()=>((state.source.code||'local').replace(/[^\w.-]/g,'_'))+'-'+date(state.bars.at(-1).time);
  async function exportSnapshot(){
    const options=readOptions();if(!options)return;const dataset=state.datasetId;let dataHash=dataDigest(state.bars),hashAlgorithm='FNV-1a (non-cryptographic)';
    if(window.crypto?.subtle){dataHash=await hashBars(state.bars,'SHA-256');hashAlgorithm='SHA-256';}
    if(dataset!==state.datasetId||JSON.stringify(options)!==JSON.stringify(readOptions(false))){toast('数据或参数已切换，请重新导出当前快照');return;}
    const record={schema:'chanxu-research-v1',exportedAt:new Date().toISOString(),runId:state.runId||null,datasetId:state.datasetId,dataHash,hashAlgorithm,source:state.source,asOf:date(state.bars.at(-1).time),ruleVersion:state.analysis.ruleVersion,options,bars:state.bars,metrics:state.backtest?.metrics||null,openPosition:state.backtest?.openPosition||null};
    download('chanxu-'+safeName()+'-research.json',JSON.stringify(record,(_k,v)=>typeof v==='number'&&!Number.isFinite(v)?String(v):v,2),'application/json;charset=utf-8');
  }
  function exportTrades(){
    if(!state.backtest)return;const buys=new Map(state.backtest.trades.filter(t=>t.side==='BUY').map(t=>[t.index,t.signalId])),options=readOptions(false);
    const rows=state.backtest.trades.filter(t=>t.side==='SELL').map(t=>({runId:state.runId,symbol:state.source.code,currency:state.source.currency,asOf:date(state.bars.at(-1).time),ruleVersion:state.analysis.ruleVersion,dataHash:dataDigest(state.bars),hashAlgorithm:'FNV-1a',parameters:JSON.stringify(options),entryDate:date(state.bars[t.entryIndex].time),signalId:buys.get(t.entryIndex),entryPrice:t.entryPrice,exitDate:date(state.bars[t.exitIndex].time),reason:t.reason,exitPrice:t.exitPrice,quantity:t.qty,pnl:t.pnl,fees:t.fees}));
    download('chanxu-'+safeName()+'-trades.csv',toCSV(rows,['runId','symbol','currency','asOf','ruleVersion','dataHash','hashAlgorithm','parameters','entryDate','signalId','entryPrice','exitDate','reason','exitPrice','quantity','pnl','fees']));
  }
  function savePrefs(){try{localStorage.setItem(PREF_KEY,JSON.stringify({version:1,symbol:$('symbol-input').value,range:$('range-input').value,sourceKind:state.source.kind,datasetId:state.datasetId,options:readOptions(false),window:state.window,end:state.end,layers:state.layers,view:state.view,selectedSignalId:state.selectedSignal?.id||null}));}catch{$('restore-status').textContent='浏览器未允许保存设置；可导出研究快照继续复盘。';}}
  function loadPrefs(){try{const p=JSON.parse(localStorage.getItem(PREF_KEY)||'null');return p?.version===1?p:null;}catch{return null;}}
  function syncWindowButtons(){document.querySelectorAll('[data-window]').forEach(b=>b.classList.toggle('selected',String(state.window)===b.dataset.window));}
  function navigate(view,persist=true){if(!['chart','watchlist','backtest','daily','rules'].includes(view))view='chart';state.view=view;document.querySelectorAll('[data-view]').forEach(b=>b.classList.toggle('active',b.dataset.view===view));['chart','watchlist','backtest','daily','rules'].forEach(v=>{const el=$(v+'-view');if(el)el.hidden=v!==view;});$('page-title').textContent=({daily:'新B点清单',chart:'结构图',watchlist:'自选股',backtest:'策略回测',rules:'规则说明'})[view];if(location.hash!=='#'+view)history.replaceState(null,'','#'+view);window.scrollTo({top:0,behavior:matchMedia('(prefers-reduced-motion:reduce)').matches?'auto':'smooth'});if(persist)savePrefs();}

  function init(){
    const saved=loadPrefs();
    if(saved){if(typeof saved.symbol==='string')$('symbol-input').value=saved.symbol.slice(0,60);if(['6mo','1y','2y','5y'].includes(saved.range))$('range-input').value=saved.range;if([60,120,'60','120','all'].includes(saved.window))state.window=saved.window;if(saved.layers)Object.keys(state.layers).forEach(k=>{if(typeof saved.layers[k]==='boolean')state.layers[k]=saved.layers[k];});setOptions(saved.options);if(!readOptions(false))setOptions(ChanEngine.DEFAULTS);$('restore-status').textContent='已恢复上次参数和视图。'+(['csv','snapshot'].includes(saved.sourceKind)?'本地文件内容不自动保存，请重新导入CSV或研究快照。':'数据将重新读取；结果需重新回测。');}
    applyDataset(state.bars,state.source,saved);syncWindowButtons();
    document.querySelectorAll('[data-layer]').forEach(b=>{b.checked=state.layers[b.dataset.layer];b.addEventListener('change',()=>{state.layers[b.dataset.layer]=b.checked;renderChart();savePrefs();});});
    $('fetch-btn').addEventListener('click',()=>fetchOnline());$('symbol-input').addEventListener('input',e=>queueSymbolSearch(e.target.value));$('symbol-input').addEventListener('keydown',symbolKeydown);
    $('symbol-input').addEventListener('compositionstart',()=>{symbolSearch.composing=true;});$('symbol-input').addEventListener('compositionend',e=>{symbolSearch.composing=false;queueSymbolSearch(e.target.value);});$('symbol-results').addEventListener('click',e=>{const option=e.target.closest('[data-symbol-choice]');if(option)chooseSymbolResult(Number(option.dataset.symbolChoice));});
    document.addEventListener('pointerdown',e=>{if(symbolSearch.open&&!e.target.closest('.symbol-search-field'))closeSymbolSearch();});
    $('common-symbol').addEventListener('change',e=>{if(e.target.value){invalidateSymbolHistory();closeSymbolSearch();$('symbol-input').value=e.target.value;}savePrefs();});$('cancel-request').addEventListener('click',cancelRequest);
    $('file-input').addEventListener('change',e=>importFile(e.target));$('snapshot-input').addEventListener('change',e=>importFile(e.target,true));
    $('demo-btn').addEventListener('click',()=>{cancelRequest();applyDataset(ChanEngine.sampleData(240),{kind:'sample',code:'DEMO',name:'合成示例',label:'合成示例 · 日线',currency:'',meta:{}});requestStatus('已切换为确定性合成示例；不代表真实行情。');savePrefs();});
    $('analyze-btn').addEventListener('click',()=>{try{const previous=state.selectedSignal?.id;state.analysis=ChanEngine.analyze(state.bars);clearSelection();resetBacktest();renderAll();const s=state.analysis.signals.find(v=>v.id===previous);if(s)selectSignal(s);toast('结构分析已更新，回测请重新运行');}catch(e){requestStatus('分析失败：'+e.message,'error');}});
    $('backtest-btn').addEventListener('click',runBacktest);
    document.querySelectorAll('.backtest-controls input').forEach(input=>input.addEventListener('input',()=>{if(state.backtest)resetBacktest();$('parameter-status').textContent='参数已修改，请重新运行回测。';input.setAttribute('aria-invalid','false');const prefix=input.id.replace('-input','');if($(prefix+'-error'))$(prefix+'-error').textContent='';savePrefs();}));$('t1-input').addEventListener('change',()=>{resetBacktest();savePrefs();});
    document.querySelectorAll('[data-window]').forEach(b=>b.addEventListener('click',()=>{state.window=b.dataset.window;state.end=null;syncWindowButtons();renderChart();savePrefs();}));
    $('reset-zoom').addEventListener('click',()=>{state.window=120;state.end=null;$('chart-date').value='';syncWindowButtons();renderChart();savePrefs();});
    $('locate-date').addEventListener('click',()=>{const v=$('chart-date').value;if(!v||v<date(state.bars[0].time)||v>date(state.bars.at(-1).time)){toast('请选择当前数据范围内的日期');$('chart-date').focus();return;}const i=state.bars.findIndex(b=>date(b.time)>=v);state.end=Math.min(state.bars.length,Math.max(5,i+1));if(state.window==='all')state.window=120;syncWindowButtons();renderChart();savePrefs();});
    $('signal-select').addEventListener('change',e=>{if(e.target.value)locateSignal(e.target.value);else{clearSelection();renderChart();savePrefs();}});document.querySelectorAll('[data-view]').forEach(b=>b.addEventListener('click',()=>navigate(b.dataset.view)));
    $('watchlist-refresh').addEventListener('click',()=>loadWatchlist({refreshMonitor:true}));$('watch-monitor-refresh').addEventListener('click',()=>scheduleWatchMonitor({force:true}));$('watch-monitor-cancel').addEventListener('click',()=>stopWatchMonitor(true));
    $('watchlist-query').addEventListener('input',e=>queueWatchSearch(e.target.value));$('watchlist-query').addEventListener('keydown',e=>{if(e.key==='Enter'){e.preventDefault();searchWatchlist(e.target.value);}});$('watchlist-search-btn').addEventListener('click',()=>searchWatchlist($('watchlist-query').value));
    $('watchlist-results').addEventListener('click',e=>{const b=e.target.closest('[data-watch-symbol]');if(b)toggleWatchlist(b.dataset.watchSymbol);});
    $('watchlist-items').addEventListener('click',e=>{const remove=e.target.closest('[data-watch-remove]');if(remove){updateWatchlist('remove',remove.dataset.watchRemove);return;}const open=e.target.closest('[data-watch-open]');if(open)openWatchChart(open.dataset.watchOpen);});
    $('chart-watch-toggle').addEventListener('click',()=>{const symbol=currentWatchSymbol();if(symbol)toggleWatchlist(symbol);});
    $('daily-refresh').addEventListener('click',loadDailyList);
    $('daily-type').addEventListener('change',e=>{daily.type=e.target.value;daily.page=1;renderDailyList();});$('daily-search').addEventListener('input',e=>{daily.query=e.target.value;daily.page=1;renderDailyList();});$('daily-clean').addEventListener('change',e=>{daily.clean=e.target.checked;daily.page=1;renderDailyList();});
    $('daily-prev').addEventListener('click',()=>{daily.page--;renderDailyList();});$('daily-next').addEventListener('click',()=>{daily.page++;renderDailyList();});$('daily-rows').addEventListener('click',e=>{const favorite=e.target.closest('[data-watch-symbol]');if(favorite){toggleWatchlist(favorite.dataset.watchSymbol);return;}const button=e.target.closest('[data-daily-code]');if(button)openDailySnapshot(button.dataset.dailyCode);});
    $('daily-export').addEventListener('click',()=>{const csv=dailyCSV();if(csv)download('chanxu-daily-'+daily.payload.report.targetDate+'-'+daily.type+'.csv',csv);});
    $('trade-rows').addEventListener('click',e=>{const b=e.target.closest('[data-signal-id]');if(b)locateSignal(b.dataset.signalId);});
    $('export-trades').addEventListener('click',exportTrades);$('export-snapshot').addEventListener('click',()=>exportSnapshot().catch(e=>toast('快照导出失败：'+e.message)));$('download-data').addEventListener('click',()=>download('chanxu-'+safeName()+'-bars.csv',toCSV(state.bars,['time','open','high','low','close','volume'])));
    $('reset-prefs').addEventListener('click',()=>{try{localStorage.removeItem(PREF_KEY);}catch{}setOptions(ChanEngine.DEFAULTS);state.window=120;state.end=null;state.layers={fractals:true,pens:true,segments:true,centers:true,signals:true};document.querySelectorAll('[data-layer]').forEach(b=>b.checked=true);syncWindowButtons();resetBacktest();renderChart();$('restore-status').textContent='已恢复默认研究参数和视图；当前数据保持不变。';});
    const resize=new ResizeObserver(()=>{if(state.analysis)renderChart();if(state.backtest)renderEquity(state.backtest.equity);});resize.observe($('chart-wrap'));resize.observe(document.querySelector('.equity-wrap'));
    syncMarketStatus('initial');setInterval(tickMarketClock,1000);setInterval(()=>{if(!document.hidden)syncMarketStatus('poll');},60000);
    loadWatchlist();setInterval(()=>{if(!document.hidden)scheduleWatchMonitor();},120000);loadDailyList();setInterval(()=>{if(!document.hidden)loadDailyList();},60000);
    window.addEventListener('focus',()=>{syncMarketStatus('focus');loadDailyList();scheduleWatchMonitor();});document.addEventListener('visibilitychange',()=>{if(!document.hidden){syncMarketStatus('visible');loadDailyList();scheduleWatchMonitor();}});
    window.addEventListener('hashchange',()=>navigate(location.hash.slice(1),false));
    const initialView=location.hash.slice(1);if(['chart','watchlist','backtest','daily','rules'].includes(initialView))navigate(initialView,false);else if(saved?.view)navigate(saved.view,false);
    if(location.protocol==='http:'&&/^(localhost|127\.0\.0\.1)$/.test(location.hostname)&&!['csv','snapshot','sample'].includes(saved?.sourceKind)){const initialRequestId=state.requestId;setTimeout(()=>loadInitialOnline(saved,initialRequestId),180);}
  }
  document.addEventListener('DOMContentLoaded',init);
})();



