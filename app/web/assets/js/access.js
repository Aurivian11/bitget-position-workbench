/* Login credentials stay in this form and the POST body only. */
(() => {
 "use strict";
 const form=document.getElementById("access-form"),input=document.getElementById("access-password"),button=document.getElementById("access-submit"),toggle=document.getElementById("access-toggle"),error=document.getElementById("access-error"),status=document.getElementById("access-status");
 let busy=false;
 const reason=new URLSearchParams(location.search).get("reason");
 if(reason==="expired"||reason==="signed-out") {status.textContent=reason==="expired"?"登录已过期，请重新登录后继续。":"你已退出持仓工作台。";status.hidden=false;}
 toggle.addEventListener("click",()=>{const show=input.type==="password";input.type=show?"text":"password";toggle.setAttribute("aria-pressed",String(show));toggle.setAttribute("aria-label",show?"隐藏密码":"显示密码");});
 form.addEventListener("submit",async event=>{
  event.preventDefault();if(busy||!form.reportValidity())return;
  busy=true;button.disabled=true;toggle.disabled=true;error.hidden=true;status.hidden=true;
  const password=input.value;input.value="";input.type="password";input.disabled=true;toggle.setAttribute("aria-pressed","false");toggle.setAttribute("aria-label","显示密码");button.querySelector("span").textContent="正在登录…";
  try{
   const response=await fetch("api/admin/login",{method:"POST",credentials:"same-origin",cache:"no-store",headers:{"Content-Type":"application/json","Accept":"application/json"},body:JSON.stringify({password}),signal:AbortSignal.timeout(15000)});
   if(!response.ok){error.textContent=response.status===401||response.status===403?"密码不正确，请重新输入。":response.status===429?"尝试次数较多，请稍后再试。":"暂时无法登录，请稍后重试。";error.hidden=false;return;}
   location.replace("./");
  }catch(_){error.textContent="无法连接到服务器，请检查网络后重试。";error.hidden=false;}
  finally{busy=false;button.disabled=false;toggle.disabled=false;input.disabled=false;button.querySelector("span").textContent="登录工作台";if(!error.hidden)input.focus();}
 });
 window.addEventListener("pagehide",()=>{input.value="";input.type="password";});
})();
