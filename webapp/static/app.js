// QA 控制台 · 轻量交互（无构建链）。HTMX 经 CDN 引入，仅此处补原生增强。
(function () {
  "use strict";

  function cookie(name) {
    return document.cookie.split(";").map(function (p) { return p.trim(); }).reduce(function (acc, part) {
      var i = part.indexOf("=");
      if (i > 0 && part.slice(0, i) === name) return decodeURIComponent(part.slice(i + 1));
      return acc;
    }, "");
  }
  function csrfName() {
    var meta = document.querySelector("meta[name=csrf-cookie]");
    return meta ? meta.getAttribute("content") : "qa_csrf";
  }
  function csrfToken() { return cookie(csrfName()); }
  function ensureHidden(form) {
    if (!form || form.method.toUpperCase() === "GET") return;
    var token = csrfToken();
    if (!token) return;
    var input = form.querySelector("input[name=csrf_token]");
    if (!input) {
      input = document.createElement("input");
      input.type = "hidden";
      input.name = "csrf_token";
      form.appendChild(input);
    }
    input.value = token;
  }
  document.addEventListener("htmx:configRequest", function (e) {
    var token = csrfToken();
    if (token) e.detail.headers["X-CSRF-Token"] = token;
  });
  document.addEventListener("submit", function (e) { ensureHidden(e.target); }, true);
  window.qaCSRFToken = csrfToken;

  // 复制命令到剪贴板
  window.copyText = function (btn, sel) {
    var el = sel ? document.querySelector(sel) : btn.previousElementSibling;
    var text = el ? (el.innerText || el.textContent) : "";
    navigator.clipboard.writeText(text.trim()).then(function () {
      var old = btn.textContent;
      btn.textContent = "已复制";
      setTimeout(function () { btn.textContent = old; }, 1500);
    });
  };

  // 复制用例源码：从 details 内的 raw textarea 取【当前值】，无需展开即可复制
  window.copyRaw = function (btn) {
    var det = btn.closest("details");
    var ta = det ? det.querySelector("textarea[name=raw]") : null;
    var text = ta ? ta.value : "";
    if (!text) return;
    navigator.clipboard.writeText(text).then(function () {
      var old = btn.textContent;
      btn.textContent = "已复制";
      setTimeout(function () { btn.textContent = old; }, 1500);
    });
  };

  // 用例树：全部展开 / 折叠
  window.treeToggleAll = function (open) {
    document.querySelectorAll(".tree details.tnode").forEach(function (d) { d.open = open; });
  };

  // 点击「问题清单」定位到节点：展开其所有祖先 <details> 再滚动高亮
  function openToAnchor(id) {
    var target = document.getElementById(id);
    if (!target) return;
    var node = target;
    while (node && node !== document.body) {
      if (node.tagName === "DETAILS") node.open = true;
      node = node.parentElement;
    }
    target.scrollIntoView({ behavior: "smooth", block: "center" });
    target.style.transition = "background .2s";
    var prev = target.style.background;
    target.style.background = "rgba(59,130,246,.18)";
    setTimeout(function () { target.style.background = prev; }, 1200);
  }
  window.openToAnchor = openToAnchor;

  document.addEventListener("click", function (e) {
    var a = e.target.closest("[data-anchor]");
    if (a) {
      e.preventDefault();
      openToAnchor(a.getAttribute("data-anchor"));
    }
  });

  // 简易表格搜索（sprint 看板）
  window.filterTable = function (input, tableSel) {
    var q = (input.value || "").toLowerCase();
    document.querySelectorAll(tableSel + " tbody tr").forEach(function (tr) {
      tr.style.display = tr.textContent.toLowerCase().indexOf(q) >= 0 ? "" : "none";
    });
  };
})();

// 全局加载反馈：顶部加载条（用户操作时）+ 被点按钮原地转圈。
// 轮询/自动加载（job 面板每 1.2s 自刷）不触发加载条，避免闪烁。
(function () {
  "use strict";
  var bar = null, pending = 0, hideTimer = null;

  function ensureBar() {
    if (bar) return bar;
    bar = document.createElement("div");
    bar.className = "load-bar";
    document.body.appendChild(bar);
    return bar;
  }
  function barStart() {
    pending++;
    var b = ensureBar();
    clearTimeout(hideTimer);
    b.classList.remove("done");
    b.classList.add("active");
    b.style.width = "0%";
    requestAnimationFrame(function () { b.style.width = "82%"; });
  }
  function barDone() {
    pending = Math.max(0, pending - 1);
    if (pending > 0 || !bar) return;
    bar.style.width = "100%";
    bar.classList.add("done");
    hideTimer = setTimeout(function () {
      bar.classList.remove("active", "done");
      bar.style.width = "0%";
    }, 280);
  }
  function isUserAction(e) {
    var rc = e.detail && e.detail.requestConfig;
    var te = rc && rc.triggeringEvent;
    return !!(te && (te.type === "click" || te.type === "submit" || te.type === "change"));
  }
  function busyBtn(elt) {
    if (!elt) return null;
    if (elt.tagName === "BUTTON") return elt;
    return elt.querySelector ? elt.querySelector("button[type=submit], button:not([type]), button[data-confirm]") : null;
  }

  document.addEventListener("htmx:beforeRequest", function (e) {
    if (isUserAction(e)) barStart();
    var b = busyBtn(e.detail.elt);
    if (b) b.classList.add("is-busy");
  });
  function clear(e) {
    if (isUserAction(e)) barDone();
    var b = busyBtn(e.detail && e.detail.elt);
    if (b) b.classList.remove("is-busy");
  }
  document.addEventListener("htmx:afterRequest", clear);
  document.addEventListener("htmx:responseError", clear);
  document.addEventListener("htmx:sendError", clear);
  document.addEventListener("htmx:timeout", clear);

  // 非 htmx 的普通表单（设置页等整页提交）：提交即禁用并转圈，给即时反馈
  document.addEventListener("submit", function (e) {
    var f = e.target;
    if (!f || f.getAttribute("hx-post") || f.getAttribute("hx-get")) return;
    var b = f.querySelector("button[type=submit], button:not([type])");
    if (b) { b.classList.add("is-busy"); b.disabled = true; }
    barStart();
  });
})();

// 设置页交互：眼睛查看明文（按需拉取）/ 获取模型列表 / 测试 Jira 连接
(function () {
  "use strict";
  function postForm(url, data) {
    var fd = new FormData();
    Object.keys(data).forEach(function (k) { fd.append(k, data[k] == null ? "" : data[k]); });
    var headers = {};
    if (window.qaCSRFToken && window.qaCSRFToken()) headers["X-CSRF-Token"] = window.qaCSRFToken();
    return fetch(url, { method: "POST", body: fd, credentials: "same-origin", headers: headers })
      .then(function (r) { return r.json(); });
  }
  function setMsg(el, text, ok) {
    if (!el) return;
    el.textContent = text;
    el.className = "field-msg " + (ok ? "ok" : "err");
  }

  function closeMenus() {
    document.querySelectorAll(".model-menu").forEach(function (m) { m.remove(); });
  }
  function openMenu(wrap) {
    if (wrap.querySelector(".model-menu")) return;
    var models = wrap.__models || [];
    if (!models.length) return;
    var modelInp = wrap.querySelector("input[name=model]");
    var menu = document.createElement("div");
    menu.className = "model-menu";
    models.forEach(function (mo) {
      var it = document.createElement("div");
      it.className = "mm-item" + (mo === modelInp.value ? " mm-sel" : "");
      it.textContent = mo;
      menu.appendChild(it);
    });
    wrap.appendChild(menu);
  }

  document.addEventListener("click", function (e) {
    // 点击 .model-wrap 之外 → 收起下拉（选项已缓存，下次点输入框可再展开）
    if (!e.target.closest(".model-wrap")) closeMenus();
    // 选中某模型 → 填入并收起
    var item = e.target.closest(".model-menu .mm-item");
    if (item) {
      item.closest(".model-wrap").querySelector("input[name=model]").value = item.textContent;
      closeMenus();
      return;
    }
    // 点击模型输入框 → 展开已缓存选项（不重新拉取）
    var mi = e.target.closest(".model-wrap input[name=model]");
    if (mi) { openMenu(mi.closest(".model-wrap")); return; }
    // 框内眼睛：切换明文/密文
    var eye = e.target.closest("[data-eye]");
    if (eye) {
      var inp = eye.closest(".secret-field").querySelector("input");
      if (inp) inp.type = (inp.type === "password") ? "text" : "password";
      return;
    }
    // 获取模型 → 拉取并缓存选项 + 展开
    var gm = e.target.closest("[data-list-models]");
    if (gm) {
      var form = gm.closest("form");
      var wrap = gm.closest(".model-wrap");
      var keyInp = form.querySelector("input[name=api_key]");
      var msg = form.querySelector("[data-model-msg]");
      gm.classList.add("is-busy");
      postForm("/settings/list-models", {
        provider: form.querySelector("[name=provider]").value,
        base_url: form.querySelector("[name=base_url]").value,
        api_key: keyInp ? keyInp.value : "",
        kind: form.getAttribute("data-kind") || ""
      }).then(function (j) {
        if (j && j.ok) {
          wrap.__models = j.models;
          setMsg(msg, "共 " + j.models.length + " 个，点输入框可再次选择", true);
          closeMenus();
          openMenu(wrap);
        } else { setMsg(msg, (j && j.error) || "获取失败", false); }
      }).catch(function () { setMsg(msg, "获取失败", false); })
        .finally(function () { gm.classList.remove("is-busy"); });
      return;
    }
    // 测试 Jira 连接
    var tj = e.target.closest("[data-test-jira]");
    if (tj) {
      var f = tj.closest("form");
      var jmsg = f.querySelector("[data-jira-msg]");
      tj.classList.add("is-busy");
      postForm("/settings/test-jira", {
        jira_url: f.querySelector("[name=jira_url]").value,
        jira_pat: f.querySelector("input[name=jira_pat]").value
      }).then(function (j) {
        if (j && j.ok) setMsg(jmsg, "已连接：" + j.name, true);
        else setMsg(jmsg, "连接失败：" + ((j && j.error) || ""), false);
      }).catch(function () { setMsg(jmsg, "连接失败", false); })
        .finally(function () { tj.classList.remove("is-busy"); });
      return;
    }
  });
})();

// 缺配置弹窗（后端 HX-Trigger: qaNeedConfig 触发）+ 设置页按 hash 定位高亮
(function () {
  "use strict";
  var CFG = {
    jira: { t: "需要 Jira 访问令牌", m: "同步 Sprint / 工单需要你的 Jira 访问令牌。现在去设置填写？", u: "/settings#cfg-jira" },
    weak: { t: "需要生成模型 API Key", m: "生成用例需要先配置「生成模型」的 API Key。现在去设置填写？", u: "/settings#cfg-weak" },
    strong: { t: "需要配置复核模型", m: "AI 复核需要先配置「复核模型」（接口地址 / API Key / 模型）。现在去设置填写？", u: "/settings#cfg-strong" }
  };
  function modal(field) {
    var c = CFG[field];
    if (!c || document.querySelector(".modal-ov[data-cfg-modal]")) return;
    var ov = document.createElement("div");
    ov.className = "modal-ov";
    ov.setAttribute("data-cfg-modal", "");
    var card = document.createElement("div");
    card.className = "modal-card";
    var t = document.createElement("div"); t.className = "modal-t"; t.textContent = c.t;
    var m = document.createElement("div"); m.className = "modal-m"; m.textContent = c.m;
    var acts = document.createElement("div"); acts.className = "modal-actions";
    var cancel = document.createElement("button"); cancel.className = "btn btn-ghost"; cancel.textContent = "取消";
    var ok = document.createElement("button"); ok.className = "btn btn-primary"; ok.textContent = "去填写";
    acts.appendChild(cancel); acts.appendChild(ok);
    card.appendChild(t); card.appendChild(m); card.appendChild(acts);
    ov.appendChild(card);
    function close() { ov.remove(); }
    cancel.addEventListener("click", close);
    ov.addEventListener("click", function (e) { if (e.target === ov) close(); });
    ok.addEventListener("click", function () { location.href = c.u; });
    document.body.appendChild(ov);
    ok.focus();
  }
  document.body.addEventListener("qaNeedConfig", function (e) {
    modal((e.detail || {}).field);
  });

  // 设置页：从弹窗跳来时按 #cfg-xxx 定位 + 聚焦 + 高亮
  var h = location.hash;
  if (h && h.indexOf("#cfg-") === 0) {
    var el = document.querySelector(h);
    if (el) {
      el.scrollIntoView({ behavior: "smooth", block: "center" });
      var inp = el.querySelector("input, select");
      setTimeout(function () { try { if (inp) inp.focus(); } catch (err) {} el.classList.add("cfg-hl"); }, 250);
      setTimeout(function () { el.classList.remove("cfg-hl"); }, 2800);
    }
  }
})();

// 通用二次确认弹窗（删除 Sprint / 重新生成）+ HTMX 载入弹窗（新增 Sprint）的关闭
(function () {
  "use strict";

  function showConfirm(opts) {
    var old = document.querySelector(".modal-ov[data-confirm-modal]");
    if (old) old.remove();
    var ov = document.createElement("div");
    ov.className = "modal-ov";
    ov.setAttribute("data-confirm-modal", "");
    var card = document.createElement("div"); card.className = "modal-card";
    var t = document.createElement("div"); t.className = "modal-t"; t.textContent = opts.title || "确认操作？";
    var m = document.createElement("div"); m.className = "modal-m"; m.textContent = opts.body || "";
    var acts = document.createElement("div"); acts.className = "modal-actions";
    var cancel = document.createElement("button");
    cancel.type = "button"; cancel.className = "btn btn-ghost btn-sm"; cancel.textContent = "取消";
    var ok = document.createElement("button");
    ok.type = "button"; ok.className = "btn btn-sm " + (opts.danger ? "btn-danger" : "btn-primary");
    ok.textContent = opts.okText || "确定";
    acts.appendChild(cancel); acts.appendChild(ok);
    card.appendChild(t); card.appendChild(m); card.appendChild(acts);
    ov.appendChild(card);
    function close() { ov.remove(); document.removeEventListener("keydown", onKey); }
    function onKey(e) { if (e.key === "Escape") close(); }
    cancel.addEventListener("click", close);
    ov.addEventListener("click", function (e) { if (e.target === ov) close(); });
    document.addEventListener("keydown", onKey);
    ok.addEventListener("click", function () { close(); if (opts.onOk) opts.onOk(); });
    document.body.appendChild(ov);
    ok.focus();
  }
  window.qaConfirm = showConfirm;

  // 带 data-confirm 的按钮（type=button）：拦截点击 → 弹确认 → 确认后提交其所在 HTMX 表单
  document.addEventListener("click", function (e) {
    var t = e.target.closest("[data-confirm]");
    if (!t) return;
    // 捕获阶段拦下：抢在 HTMX 的 click 触发之前阻止，确认后才真正发请求
    // （否则带 hx-post 的按钮在 click 目标阶段已发出请求，弹窗形同虚设）
    e.preventDefault();
    e.stopPropagation();
    if (e.stopImmediatePropagation) e.stopImmediatePropagation();
    showConfirm({
      title: t.getAttribute("data-confirm-title") || "确认操作？",
      body: t.getAttribute("data-confirm-body") || "",
      okText: t.getAttribute("data-confirm-ok") || "确定",
      danger: t.hasAttribute("data-confirm-danger"),
      onOk: function () {
        var f = t.closest("form");
        if (f) {
          if (f.requestSubmit) f.requestSubmit();
          else if (window.htmx) window.htmx.trigger(f, "submit");  // 老浏览器(Safari<16)回退
          return;
        }
        // 无 form：按钮自身带 hx-post/hx-get（如工单行内操作），用 htmx.ajax 触发（click 已被拦截）
        var verb = t.getAttribute("hx-post") ? "POST" : (t.getAttribute("hx-get") ? "GET" : null);
        var path = t.getAttribute("hx-post") || t.getAttribute("hx-get");
        if (verb && path && window.htmx) {
          var vals = {};
          try { vals = JSON.parse(t.getAttribute("hx-vals") || "{}"); } catch (e) {}
          window.htmx.ajax(verb, path, {
            source: t, target: t.getAttribute("hx-target") || undefined,
            swap: t.getAttribute("hx-swap") || undefined, values: vals
          });
        }
      }
    });
  }, true);

  // 新增 Sprint 弹窗（HTMX 载入的 .modal-ov[data-modal]）：取消按钮 / 点遮罩 / Esc 关闭
  document.addEventListener("click", function (e) {
    if (e.target.closest("[data-modal-close]")) {
      var mc = e.target.closest(".modal-ov[data-modal]"); if (mc) mc.remove(); return;
    }
    if (e.target.classList && e.target.classList.contains("modal-ov") && e.target.hasAttribute("data-modal")) {
      e.target.remove();
    }
  });
  document.addEventListener("keydown", function (e) {
    if (e.key !== "Escape") return;
    var m = document.querySelector(".modal-ov[data-modal]"); if (m) m.remove();
  });
})();

// 作业实时态：作业运行中即刻禁用看板上会启动新作业的按钮（无需刷新）；运行中→完成 自动刷新一次，
// 让状态/计数/按钮随之更新（用户反馈：操作完/运行中页面状态不变、必须手动刷新）。
(function () {
  "use strict";
  var wasRunning = false;
  function isRunning() {
    return !!document.querySelector("#job-panel .j-status.running");
  }
  function sync() {
    var run = isRunning();
    document.body.classList.toggle("job-running", run);
    if (run) { wasRunning = true; return; }
    if (wasRunning) {                 // 运行中 → 已结束：刷新一次，让看板状态/按钮/计数更新
      wasRunning = false;
      setTimeout(function () { location.reload(); }, 900);  // 留点时间看到“已完成”
    }
  }
  // 作业面板每次轮询替换 / 作业区载入后，都重新同步禁用态与完成检测
  document.addEventListener("htmx:afterSwap", sync);
  document.addEventListener("DOMContentLoaded", function () {
    wasRunning = isRunning();
    document.body.classList.toggle("job-running", wasRunning);
  });
})();

// 修复——替换前记下是否贴底；替换后：首次出现/原本贴底 → 跟到最新(底部)；用户上翻看历史 → 保留其位置。
(function () {
  "use strict";
  var had = false, stick = true, prevTop = 0;
  function jl(scope) { return (scope || document).querySelector(".joblog"); }
  document.addEventListener("htmx:beforeSwap", function (e) {
    var t = e.detail && e.detail.target;
    if (!t || t.id !== "job-panel") { had = false; return; }   // 仅处理 job-panel 自轮询替换
    var log = jl(t);
    had = !!log;
    if (log) {
      stick = (log.scrollHeight - log.scrollTop - log.clientHeight) < 24;  // 距底 <24px 视为“跟随中”
      prevTop = log.scrollTop;
    }
  });
  document.addEventListener("htmx:afterSwap", function () {
    var log = document.querySelector("#job-panel .joblog");
    if (!log) return;
    if (!had || stick) log.scrollTop = log.scrollHeight;   // 首次/原本贴底 → 跟最新
    else log.scrollTop = prevTop;                          // 上翻看历史 → 保留位置
  });
})();

