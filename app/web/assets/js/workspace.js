/* Navigation, notifications and credential-safe configuration. */
(() => {
  "use strict";
  const el = id => document.getElementById(id);
  const html = value => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  const ui = {view:"dashboard",csrf:null,settings:null,dirty:false,eventPage:1,events:[],busy:false,applying:false,sessionChecked:false,notificationLoading:false,authEpoch:0};
  const titles = {dashboard:"账户总览",notifications:"通知中心",settings:"通知配置",guide:"配置教程"};
  const names = {telegram:"Telegram",discord:"Discord",bark:"Bark"};
  const icons = {telegram:"send",discord:"messages-square",bark:"bell-ring"};
  const eventNames = {opened:"开仓",increased:"加仓",reduced:"减仓",closed:"平仓",updated:"参数变更"};
  const iconsNow = () => window.lucide?.createIcons();
  const datetime = (value, seconds = false) => {
    if (value === null || value === undefined || value === "") return "—";
    const d = new Date(seconds ? Number(value)*1000 : value);
    return Number.isNaN(d.getTime()) ? "—" : new Intl.DateTimeFormat("zh-CN",{timeZone:"Asia/Shanghai",month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit",second:"2-digit",hourCycle:"h23"}).format(d);
  };
  async function request(path, body) {
    const options = {cache:"no-store",credentials:"same-origin",headers:{"Accept":"application/json"},signal:AbortSignal.timeout(25000)};
    if (body !== undefined) {
      options.method = "POST"; options.headers["Content-Type"] = "application/json";
      if (ui.csrf) options.headers["X-CSRF-Token"] = ui.csrf;
      options.body = JSON.stringify(body);
    }
    const response = await fetch("api/"+path,options);
    if(path!=="admin/login" && window.BitgetSession?.check(response)===false){const error=new Error("登录已过期，请重新登录。");error.status=401;throw error;}
    let data;
    try { data = await response.json(); } catch (_) { throw new Error("服务暂时未响应，请稍后重试。"); }
    if (!response.ok) {
      const error = new Error(data.error || data.message || "请求未完成，请稍后重试。");
      error.status = response.status; throw error;
    }
    return data;
  }
  let toastTimer;
  function toast(text) {
    el("toast").textContent=text;el("toast").hidden=false;
    clearTimeout(toastTimer);toastTimer=setTimeout(()=>el("toast").hidden=true,4500);
  }
  function feedback(text, tone="") {
    const box=el("settings-feedback");box.textContent=text;box.className="notice "+tone;box.hidden=false;
  }
  function setConfigBusy(busy) {
    el("settings-form").querySelectorAll("input,select,button").forEach(control => { control.disabled = busy; });
    el("save-settings").disabled = busy || !ui.dirty;
  }
  function resetSession(message = "") {
    ui.authEpoch++; ui.csrf=null; ui.settings=null; ui.dirty=false;
    el("settings-channels").replaceChildren();
    el("settings-content").hidden=true; el("login-panel").hidden=false;
    el("admin-password").value=""; el("settings-feedback").hidden=true;
    el("login-error").textContent=message; el("login-error").hidden=!message;
    window.BitgetSession?.leave("expired");
  }
  function notificationHealth(message, tone = "") {
    let notice=el("notification-health");
    if (!notice) { notice=document.createElement("div");notice.id="notification-health";notice.setAttribute("role","status");el("channel-status-grid").before(notice); }
    notice.className="notice "+tone;notice.textContent=message;
  }
  function route() {
    const hash=location.hash.slice(1);
    const view=hash.startsWith("guide")?"guide":Object.hasOwn(titles,hash)?hash:"dashboard";
    const changed=ui.view!==view;
    ui.view=view;document.body.dataset.workspace=view;
    Object.keys(titles).forEach(v=>el(v+"-view").hidden=v!==view);
    document.querySelectorAll(".sidebar nav a").forEach(a=>{
      const active=view!=="dashboard" && a.dataset.view===view;
      a.classList.toggle("active",active);
      if(active)a.setAttribute("aria-current","page");else a.removeAttribute("aria-current");
    });
    el("workspace-title").textContent=titles[view];
    if(view==="dashboard"){
      if(typeof activateNavigation==="function")activateNavigation(["account","positions","ledger","performance"].includes(hash)?hash:"account");
      if(changed){window.dispatchEvent(new Event("resize"));setTimeout(()=>el(hash)?.scrollIntoView({block:"start"}),30);}
    }else if(changed){window.scrollTo({top:0,behavior:"instant"});}
    if(view==="notifications")loadNotifications();
    if(view==="settings")loadSession();
    if(view==="guide" && hash.startsWith("guide-"))setTimeout(()=>el(hash)?.scrollIntoView({block:"start"}),50);
    iconsNow();
  }
  window.addEventListener("hashchange",route);
  document.addEventListener("click",e=>{
    const link=e.target.closest("a[data-view]");
    if(link && link.hash===location.hash){e.preventDefault();route();if(link.dataset.view==="dashboard")el(link.hash.slice(1))?.scrollIntoView({block:"start",behavior:"smooth"});}
  });
  async function loadNotifications(){
    if (ui.notificationLoading) return;
    ui.notificationLoading=true;
    el("refresh-events").disabled=true;
    try{
      const [status,events]=await Promise.all([request("notification-status"),request("events")]);
      renderStatus(status);
      ui.events=Array.isArray(events.events)?events.events:[];
      renderEvents();
    }catch(error){
      notificationHealth("刷新失败；下方如有记录，为上一次读取的结果。请稍后重试。","warning");
      if(!ui.events.length)el("event-list").innerHTML='<div class="empty-panel"><i data-lucide="wifi-off"></i><strong>暂时无法读取通知记录</strong><span>'+html(error.message)+'</span></div>';
      else toast("记录刷新失败，保留上一次数据。");
      if(!el("channel-status-grid").querySelector(".status-card"))el("channel-status-grid").innerHTML='<div class="surface loading-panel">通知状态暂时不可用，请稍后刷新。</div>';
      iconsNow();
    }finally{ui.notificationLoading=false;el("refresh-events").disabled=false;}
  }
  function renderStatus(data){
    notificationHealth(data.monitorHealthy ? "持仓监控同步正常 · 最近确认 "+datetime(data.monitorHeartbeatAt,true) : "尚未确认最新持仓同步，请查看总览或稍后刷新。",data.monitorHealthy ? "success" : "warning");
    el("channel-status-grid").innerHTML=Object.entries(names).map(([key,name])=>{
      const c=data.channels?.[key]||{};
      const enabled=c.enabled&&c.configured;
      const badge=!c.enabled?"已停用":!c.configured?"待补充凭据":"已启用";
      const date=c.lastAcceptedAt?datetime(c.lastAcceptedAt,true):(key==="bark"?"暂无成功回执":"当前通道未统计确认时间");
      const note=c.uncertain?String(c.uncertain)+" 条结果待核对":key==="bark"?"开仓成交提醒":"持仓卡片与事件消息";
      return '<article class="surface status-card"><div class="channel-heading"><span class="channel-icon '+key+'"><i data-lucide="'+icons[key]+'"></i></span><div><h2>'+name+'</h2><p>'+note+'</p></div></div><span class="status-badge '+(enabled?"":c.enabled?"waiting":"off")+'">'+badge+'</span><div class="status-counts"><div><strong>'+(c.pending??"—")+'</strong><span>待处理</span></div><div><strong>'+(c.failed??"—")+'</strong><span>历史异常</span></div></div><div class="status-foot">最近接口确认：'+html(date)+'<br>启用状态不等于消息已送达设备</div></article>';
    }).join("");iconsNow();
  }
  function renderEvents(){
    const query=el("event-search").value.trim().toUpperCase(),type=el("event-type").value;
    const rows=ui.events.filter(e=>(!query||String(e.symbol).toUpperCase().includes(query))&&(type==="all"||e.type===type));
    el("events-count").textContent=rows.length+" / "+ui.events.length+" 条最近事件";
    const states={processed:"已处理",pending:"待处理",skipped:"已跳过",unknown:"状态未知"};
    const pages=Math.max(1,Math.ceil(rows.length/12));ui.eventPage=Math.min(ui.eventPage,pages);
    el("event-page").textContent=ui.eventPage+" / "+pages;el("event-prev").disabled=ui.eventPage<=1;el("event-next").disabled=ui.eventPage>=pages;
    el("event-list").innerHTML=rows.length?rows.slice((ui.eventPage-1)*12,ui.eventPage*12).map(e=>{
      const direction=e.side==="long"?"做多":e.side==="short"?"做空":"—";
      const glyph=e.type==="closed"?"check":e.type==="reduced"?"minus":e.type==="updated"?"settings-2":"arrow-up-right";
      return '<div class="event-row"><span class="event-type-icon"><i data-lucide="'+glyph+'"></i></span><div><div class="event-symbol">'+html(e.symbol)+'</div><div class="event-meta">'+direction+' · 数量 '+html(e.size??"—")+'</div></div><span class="event-kind">'+html(eventNames[e.type]||e.type)+'</span><time class="event-time">'+datetime(e.createdAt)+'</time><span class="event-tag">TG · '+(states[e.telegramState]||"状态未知")+'</span></div>';
    }).join(""):'<div class="empty-panel"><i data-lucide="list-filter"></i><strong>'+(ui.events.length?"没有符合条件的记录":"暂无持仓变动")+'</strong><span>'+(ui.events.length?"调整合约或事件类型后重试。":"监控捕捉到新事件后，将在这里显示。")+'</span></div>';
    iconsNow();
  }
  el("event-search").addEventListener("input",()=>{ui.eventPage=1;renderEvents();});el("event-type").addEventListener("change",()=>{ui.eventPage=1;renderEvents();});
  el("event-prev").addEventListener("click",()=>{ui.eventPage--;renderEvents();});el("event-next").addEventListener("click",()=>{ui.eventPage++;renderEvents();});el("refresh-events").addEventListener("click",loadNotifications);
  async function loadSession(){
    const local=["127.0.0.1","localhost"].includes(location.hostname);
    if(location.protocol!=="https:"&&!local){
      el("https-required").hidden=false;el("login-panel").hidden=true;el("settings-content").hidden=true;return;
    }
    el("https-required").hidden=true;
    const epoch=ui.authEpoch;
    try{
      const session=await request("admin/session");if(epoch!==ui.authEpoch)return;ui.sessionChecked=true;ui.csrf=session.csrfToken||null;
      if(session.authenticated){
        el("login-panel").hidden=true;el("settings-content").hidden=false;
        if(!ui.settings || !ui.dirty && !ui.applying && !ui.busy){
          const next=await request("admin/settings");
          if(epoch!==ui.authEpoch)return;
          if(!ui.dirty && !ui.applying && !ui.busy){ui.settings=next;renderSettings();}
        }
      }else{resetSession();}
    }catch(error){
      el("login-panel").hidden=false;el("settings-content").hidden=true;
      el("login-error").textContent=error.message;el("login-error").hidden=false;
      window.BitgetSession?.verify();
    }iconsNow();
  }
  el("login-form").addEventListener("submit",async e=>{
    e.preventDefault();const btn=e.target.querySelector('[type="submit"]');btn.disabled=true;el("login-error").hidden=true;
    try{
      const result=await request("admin/login",{password:el("admin-password").value});
      ui.authEpoch++;ui.csrf=result.csrfToken;el("admin-password").value="";ui.settings=null;ui.dirty=false;await loadSession();
    }catch(error){el("login-error").textContent=error.message;el("login-error").hidden=false;}
    finally{btn.disabled=false;}
  });
  el("toggle-password").addEventListener("click",()=>{
    const input=el("admin-password"),show=input.type==="password";input.type=show?"text":"password";
    el("toggle-password").setAttribute("aria-label",show?"隐藏密码":"显示密码");
    el("toggle-password").innerHTML='<i data-lucide="'+(show?"eye-off":"eye")+'"></i>';iconsNow();
  });
  el("logout").addEventListener("click",async()=>{
    if(window.BitgetSession){await window.BitgetSession.logout();return;}
    try{await request("admin/logout",{});resetSession();toast("已退出通知管理");}
    catch(error){if(error.status===401){resetSession("登录已过期，请重新登录。");}else{toast(error.message);}}
  });
  const switchHTML=(channel,key,checked,label)=>'<label class="switch-control"><input type="checkbox" data-channel="'+channel+'" data-field="'+key+'" '+(checked?"checked":"")+' aria-label="'+html(label)+'"><span class="switch-track"></span></label>';
  function field(channel,key,label,value,hint="",options={}){
    const secret=options.secret,configured=options.configured;
    const attrs=options.type==="number"?'type="number" min="'+options.min+'" max="3600" step="1"':'type="'+(secret?"password":"text")+'"';
    return '<div class="form-field"><label for="'+channel+'-'+key+'">'+label+(secret?'<span class="configured-tag">'+(configured?"已配置":"尚未配置")+'</span>':"")+'</label><input id="'+channel+'-'+key+'" data-channel="'+channel+'" data-field="'+key+'" '+attrs+' value="'+html(secret?"":value??"")+'" maxlength="'+(secret?2048:500)+'" autocomplete="'+(secret?"new-password":"off")+'" spellcheck="false" placeholder="'+html(secret?(configured?"留空保留现有凭据":"填写你的"+label):options.placeholder||"")+'">'+(hint?'<p class="form-hint">'+hint+'</p>':"")+'</div>';
  }
  function settingsCard(key){
    const c=ui.settings.channels[key],name=names[key];
    let body="",subtitle=key==="bark"?"开仓成交 · 手机提醒":"实时持仓 · 交易事件";
    if(key==="telegram"){
      body='<div class="form-fields">'+field(key,"token","Bot Token","", "仅在需要更换机器人时填写新 Token。",{secret:true,configured:c.token_configured})+field(key,"chat_id","目标 Chat ID",c.chat_id,"支持数字 ID；群组 ID 通常为负数。")+'</div>';
    }else if(key==="discord"){
      body='<div class="form-fields">'+field(key,"webhook","Webhook 地址","","填写目标频道的完整 Webhook URL，已有地址不会回显。",{secret:true,configured:c.webhook_configured})+'</div>';
    }else{
      body='<div class="form-fields">'+field(key,"base","推送服务器地址",c.base,"填写服务器根地址，不包含设备 Key。",{placeholder:"https://api.day.app"})+field(key,"key","设备 Key","","使用所选服务器对应的设备 Key。",{secret:true,configured:c.key_configured})+field(key,"sound","提醒声音",c.sound,"填写 Bark 支持的声音名称或已导入的铃声。")+field(key,"group","通知分组",c.group,"在 Bark 历史记录中归类这些消息。")+field(key,"reconcile_seconds","成交核对间隔（秒）",c.reconcile_seconds,"定期核对遗漏的成交，WebSocket 仍可实时触发。",{type:"number",min:5})+'</div><div class="settings-divider"></div><div class="setting-row"><div><strong>持续响铃提醒</strong><p>使用 Bark 的 call 选项；实际提醒受手机通知权限影响。</p></div>'+switchHTML(key,"call",c.call,"Bark 持续响铃提醒")+'</div><p class="form-hint">Bark 当前仅处理开仓方向的成交，不推送减仓、平仓或持仓看板。</p>';
    }
    if(key!=="bark"){
      body+='<div class="settings-divider"></div><div class="setting-row"><div><strong>开仓与实时持仓卡片</strong><p>开仓后建立卡片，持续更新价格、盈亏及持仓参数。</p></div>'+switchHTML(key,"board_enabled",c.board_enabled,name+" 开仓与实时持仓卡片")+'</div><div class="form-field"><label>额外交易提醒</label><div class="event-options">'+["increased","reduced","closed"].map(type=>'<label class="event-option"><input type="checkbox" data-channel="'+key+'" data-event="'+type+'" '+(c.events.includes(type)?"checked":"")+'>'+eventNames[type]+'</label>').join("")+'</div><p class="form-hint">开仓及参数更新由上方持仓卡片管理。关闭卡片不会自动关闭已选的交易提醒。</p></div><div class="settings-divider"></div>'+field(key,"update_seconds","持仓卡片刷新间隔（秒）",c.update_seconds,"目标间隔为 1–3600 秒；网络耗时及平台限流会使实际间隔延长。",{type:"number",min:1});
    }
    return '<article class="surface settings-card '+key+'-card"><div class="channel-heading"><span class="channel-icon '+key+'"><i data-lucide="'+icons[key]+'"></i></span><div><h2>'+name+'</h2><p>'+subtitle+'</p></div>'+switchHTML(key,"enabled",c.enabled,"启用 "+name)+'</div>'+body+'<div class="settings-card-footer"><span>关闭渠道保留凭据，不补发停用期间旧事件</span><a href="#guide-'+key+'" data-view="guide">获取配置<i data-lucide="arrow-up-right"></i></a></div></article>';
  }
  function renderSettings(){
    el("settings-channels").innerHTML=["telegram","discord","bark"].map(settingsCard).join("");
    ui.dirty=false;el("dirty-status").textContent="当前配置已同步";
    updateRevision(ui.settings);
    setConfigBusy(ui.busy || ui.applying);iconsNow();
  }
  function updateRevision(data){
    const applied=data.appliedRevision===data.revision&&!data.applying;
    const fresh=data.monitorHeartbeatAt && Date.now()/1000-data.monitorHeartbeatAt<90;
    el("config-revision").textContent="配置 v"+data.revision+" · "+(applied?(fresh?"监控已加载":"已加载 · 同步待确认"):"等待监控确认");
  }
  el("settings-form").addEventListener("input",()=>{
    if(ui.busy || ui.applying)return;
    ui.dirty=true;el("dirty-status").textContent="有未保存的更改";el("save-settings").disabled=false;
  });
  el("discard-settings").textContent="重新读取";
  el("discard-settings").title="放弃未保存的更改，读取服务器上的最新配置";
  el("discard-settings").addEventListener("click",async()=>{
    if(ui.busy || ui.applying)return;
    ui.busy=true;setConfigBusy(true);
    try{
      const next=await request("admin/settings");ui.settings=next;renderSettings();el("settings-feedback").hidden=true;toast("已重新读取服务器配置");
    }catch(error){if(error.status===401)resetSession("登录已过期，请重新登录。");else feedback(error.message,"error");}
    finally{ui.busy=false;setConfigBusy(false);}
  });
  function collect(){
    const channels={telegram:{events:[]},discord:{events:[]},bark:{}};
    el("settings-channels").querySelectorAll("[data-field]").forEach(input=>{
      let value=input.type==="checkbox"?input.checked:input.type==="number"?Number(input.value):input.value.trim();
      channels[input.dataset.channel][input.dataset.field]=value;
    });
    el("settings-channels").querySelectorAll("[data-event]:checked").forEach(input=>channels[input.dataset.channel].events.push(input.dataset.event));
    return {revision:ui.settings.revision,channels};
  }
  async function waitApplied(revision){
    ui.applying=true;
    const epoch=ui.authEpoch;
    const start=Date.now();
    while(Date.now()-start<90000){
      await new Promise(r=>setTimeout(r,2500));
      if(epoch!==ui.authEpoch){ui.applying=false;return;}
      try{
        const data=await request("admin/settings");if(epoch!==ui.authEpoch){ui.applying=false;return;}updateRevision(data);
        if(data.revision!==revision){ui.applying=false;feedback("配置已被另一个页面更新，请点击“重新读取”后核对。","warning");return;}
        if(data.appliedRevision===revision&&!data.applying){
          ui.settings=data;
          feedback("配置已生效，监控已完成一次同步。消息投递情况可在通知中心查看。","success");ui.applying=false;return;
        }
      }catch(error){if(error.status===401){ui.applying=false;resetSession("配置已保存。登录已过期，请重新登录查看生效状态。");return;}}
    }
    ui.applying=false;feedback("配置已保存，仍在等待监控确认。请稍后刷新本页核对状态。","warning");
  }
  el("settings-form").addEventListener("submit",async e=>{
    e.preventDefault();if(ui.busy||ui.applying)return;
    if(!ui.settings || !ui.dirty || !e.target.reportValidity())return;
    ui.busy=true;setConfigBusy(true);
    const epoch=ui.authEpoch,previousRevision=ui.settings.revision;
    try{
      const body=collect(),data=await request("admin/settings",body);
      if(epoch!==ui.authEpoch)return;
      ui.settings=data;renderSettings();setConfigBusy(true);
      if(data.revision===previousRevision && !data.applying){feedback("当前设置已经是最新，无需重新加载监控。","success");}
      else{feedback("配置已保存，正在等待监控重新连接并确认新配置。");await waitApplied(data.revision);}
    }catch(error){
      feedback(error.status===409?"配置已在其他页面修改。请点击“重新读取”后再编辑，以免覆盖更新。":error.message,"error");
      if(error.status===401){resetSession("登录已过期，未保存的配置已清除，请重新登录。");}
    }finally{ui.busy=false;setConfigBusy(ui.applying);}
  });
  window.addEventListener("bitget:session-ended",()=>{ui.authEpoch++;ui.csrf=null;ui.settings=null;ui.dirty=false;ui.applying=false;el("settings-channels").replaceChildren();el("admin-password").value="";});
  window.addEventListener("beforeunload",event=>{if(ui.dirty&&!window.BitgetSession?.leaving){event.preventDefault();event.returnValue="";}});
  setInterval(()=>{if(ui.view==="notifications"&&!document.hidden)loadNotifications();},30000);
  route();
})();
