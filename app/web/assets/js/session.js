/* Site-wide session gate. No browser persistence; restored pages revalidate. */
(() => {
 "use strict";
 const root=document.documentElement,base="/bitget/";
 let leaving=false,exitReason="expired",checking=null,logoutBusy=false,logoutPending=false,epoch=0,retryAction=null;
 const shield=document.getElementById("session-shield");
 const title=shield.querySelector("strong"),note=shield.querySelector("p"),retry=shield.querySelector("button");
 function cover(label="正在确认登录状态",message="请稍候，正在安全连接工作台。"){
  root.dataset.sessionState="checking";title.textContent=label;note.textContent=message;retry.hidden=true;
 }
 function failed(message,action){
  root.dataset.sessionState="error";title.textContent="暂时无法确认登录状态";note.textContent=message;retryAction=action;retry.textContent=logoutPending?"重试退出":"重新验证";retry.hidden=false;
 }
 retry.addEventListener("click",()=>retryAction?.());
 function leave(reason="expired"){
  if(leaving)return;leaving=true;epoch++;
  cover(reason==="signed-out"?"已退出工作台":"登录已过期","正在返回登录页面。");
  document.querySelectorAll('input[type="password"]').forEach(input=>{input.value="";});
  window.dispatchEvent(new Event("bitget:session-ended"));
  exitReason=reason==="signed-out"?"signed-out":"expired";
  location.replace(base+"?reason="+exitReason);
 }
 function check(response){if(response.status===401){leave("expired");return false;}return !leaving;}
 async function session(){
  const response=await fetch(base+"api/admin/session",{credentials:"same-origin",cache:"no-store",headers:{Accept:"application/json"},signal:AbortSignal.timeout(10000)});
  if(response.status===401)return {authenticated:false};
  if(!response.ok)throw new Error("session unavailable");
  const data=await response.json();
  if(typeof data.authenticated!=="boolean")throw new Error("session invalid");
  return data;
 }
 function verify(){
  if(leaving||logoutPending)return Promise.resolve(false);
  if(checking)return checking;
  cover();const version=++epoch;
  const task=(async()=>{
   try{
    const data=await session();if(version!==epoch||leaving||logoutBusy)return false;
    if(!data.authenticated){leave("expired");return false;}
    delete root.dataset.sessionState;window.dispatchEvent(new Event("resize"));return true;
   }catch(_){if(version===epoch&&!leaving)failed("私密内容已隐藏。请检查网络，再重新验证登录状态。",verify);return false;}
   finally{if(checking===task)checking=null;}
  })();checking=task;return task;
 }
 async function logout(){
  if(leaving||logoutBusy)return;logoutBusy=true;logoutPending=true;epoch++;
  cover("正在退出工作台","正在结束当前登录会话。");
  try{
   const data=await session();if(!data.authenticated){leave("signed-out");return;}
   if(!data.csrfToken)throw new Error("csrf unavailable");
   const response=await fetch(base+"api/admin/logout",{method:"POST",credentials:"same-origin",cache:"no-store",headers:{"Content-Type":"application/json","X-CSRF-Token":data.csrfToken,Accept:"application/json"},body:"{}",signal:AbortSignal.timeout(10000)});
   if(response.status===401||response.ok){leave("signed-out");return;}
   throw new Error("logout unavailable");
  }catch(_){failed("尚未确认退出，私密内容已隐藏。请检查网络后重试退出。",logout);retry.textContent="重试退出";}
  finally{logoutBusy=false;}
 }
 window.BitgetSession={check,leave,logout,verify,get leaving(){return leaving;}};
 document.getElementById("site-logout")?.addEventListener("click",logout);
 window.addEventListener("pagehide",()=>{epoch++;checking=null;cover();});
 function resume(){if(leaving){location.replace(base+"?reason="+exitReason);return;}if(logoutPending){failed("尚未确认退出，私密内容已隐藏。请检查网络后重试退出。",logout);return;}cover();verify();}
 window.addEventListener("pageshow",resume);
 document.addEventListener("visibilitychange",()=>{
  if(document.hidden){epoch++;checking=null;cover();}
  else resume();
 });
 cover();verify();
})();
