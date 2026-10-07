/* 看有 共用互動(無框架、無外部資源)。
   網頁已開嚴格 CSP(W2-B,不允許行內 script),所以不寫任何行內 JS:行為一律用 data-* 屬性掛在這裡。
   沒有 JS 時表單照樣能送出(伺服器端會檢查),這裡只是讓等待與錯誤更清楚。 */
(function () {
  "use strict";

  // ---- data-confirm:不可逆動作前先問一次(取代舊樣板的 onsubmit) ----
  // 用 base.html 的確認框(<dialog> + showModal():焦點留在框內、Esc 關閉、有遮罩),不用瀏覽器內建的確認視窗
  // (樣子和網頁不一樣,按鈕還寫「取消」,和「取消提醒」撞字)。按鈕或表單帶 data-confirm-title(標題)、
  // data-confirm(說明)、data-confirm-ok(確定鍵,沒寫就用框裡原本的「確定」)、
  // data-confirm-field(伺服器要看到才做的確認欄位,按「確定」後才補進表單)。
  // 按「確定」才重新送出;「先不要」、Esc、點遮罩都是不做,焦點回到原本的按鈕。
  // 不支援 <dialog> 或 requestSubmit 的舊瀏覽器(例如 iOS 15 的 Safari)退回用 window.confirm,功能不壞。
  var dialog = document.querySelector("[data-confirm-dialog]");
  var part = function (name) { return dialog && dialog.querySelector("[data-dialog-" + name + "]"); };
  var dialogTitle = part("title"), dialogMsg = part("msg"), dialogNo = part("no"), dialogOk = part("ok");
  var okLabel = part("ok-label");
  var useDialog = Boolean(dialogTitle && dialogMsg && dialogNo && dialogOk && okLabel &&
                          typeof dialog.showModal === "function" &&
                          window.HTMLFormElement && typeof HTMLFormElement.prototype.requestSubmit === "function");
  var okDefault = okLabel ? okLabel.textContent : "";
  var asking = null;      // 框開著時:{ back: 關掉後焦點回去的元素, send: 按「確定」後怎麼重新送出, at: 打開的時間 }
  var approved = false;   // 按「確定」後重新送出的那一次放行,不再攔

  // 伺服器也要看到「確認過了」才做:按了「確定…」才把 data-confirm-field 指定的欄位(值是 1)補進表單。
  // 沒有 JS、或這支程式沒跑起來時表單裡沒有這個欄位,伺服器不會直接生效,改用一頁確認頁再問一次
  function markConfirmed(trigger, form) {
    var name = trigger.getAttribute("data-confirm-field");
    if (!name || !form || form.querySelector('input[name="' + name + '"]')) return;
    var field = document.createElement("input");
    field.type = "hidden";
    field.name = name;
    field.value = "1";
    form.appendChild(field);
  }

  function confirmFirst(event, trigger, form, back, send) {
    if (approved) return;
    var title = trigger.getAttribute("data-confirm-title") || "";
    var text = trigger.getAttribute("data-confirm") || "";
    if (!useDialog) {
      if (window.confirm(title && text ? title + "\n" + text : title || text)) markConfirmed(trigger, form);
      else event.preventDefault();
      return;
    }
    event.preventDefault();
    dialogTitle.textContent = title || text;   // 只寫 data-confirm 的舊寫法:整句當標題
    dialogMsg.textContent = title ? text : "";
    dialogMsg.hidden = !dialogMsg.textContent;
    okLabel.textContent = trigger.getAttribute("data-confirm-ok") || okDefault;
    asking = { back: back, at: Date.now(), send: function () { markConfirmed(trigger, form); send(); } };
    dialog.showModal();   // 焦點落在「先不要」(autofocus)
  }

  // 剛打開的半秒內,點框上的按鈕或遮罩都不算數:長輩常連點兩下「退回」,第二下會落在框上——
  // 剛好是「確定退回」就等於沒看就退回,是遮罩就變成框一閃就關。Esc 不受影響
  function decide(ok) {
    if (asking && Date.now() - asking.at >= 500) finish(ok);
  }

  function finish(ok) {
    var done = asking;
    asking = null;
    if (dialog.open) dialog.close();
    if (!done) return;
    if (done.back && typeof done.back.focus === "function") done.back.focus();
    if (!ok) return;
    approved = true;
    try { done.send(); } finally { approved = false; }   // requestSubmit 當場就觸發 submit,送完立刻收回放行
  }

  if (useDialog) {
    dialogNo.addEventListener("click", function () { decide(false); });
    dialogOk.addEventListener("click", function () { decide(true); });
    // Esc 由瀏覽器直接關框;框已經又被打開(上一次的 close 事件晚到)就不理
    dialog.addEventListener("close", function () { if (!dialog.open) finish(false); });
    // 點遮罩:事件落在 <dialog> 本身而且在框外(點框裡的空白也落在 <dialog>,不算)
    dialog.addEventListener("click", function (event) {
      if (event.target !== dialog) return;
      var box = dialog.getBoundingClientRect();
      if (event.clientX < box.left || event.clientX > box.right ||
          event.clientY < box.top || event.clientY > box.bottom) decide(false);
    });
  }

  // 整張表單都要先問(例如更正頁的「退回(品質不足)」);重新送出時帶上原本按的那顆按鈕
  document.querySelectorAll("form[data-confirm]").forEach(function (form) {
    form.addEventListener("submit", function (event) {
      var submitter = event.submitter;
      confirmFirst(event, form, form, submitter || document.activeElement, function () {
        if (submitter) form.requestSubmit(submitter); else form.requestSubmit();
      });
    });
  });
  // 同一張表單裡只有某顆按鈕要先問(例如家人確認的「退回」),掛在按鈕上;
  // 重新送出要指定這顆按鈕,它的 name/value(decision=rejected)才會一起送出
  document.querySelectorAll("button[data-confirm]").forEach(function (button) {
    button.addEventListener("click", function (event) {
      confirmFirst(event, button, button.form, button, function () { button.form.requestSubmit(button); });
    });
  });

  // ---- data-send-once:一張表單只送出一次(更正頁的「存檔並重新核對」) ----
  // 存檔要重新核對(發票要解 QR Code),等的時候常會再按一下;兩個請求並行會各搬一次原件,所以第一次送出後,
  // 後面的一律擋下。按鈕要等這次送出的資料收好(下一輪)才停用:停用的按鈕不會被送出,當場停用的話
  // 「再加一種藥」的 add_item 會不見,伺服器就當成存檔了。帶 data-send-quiet 的按鈕(再加一種藥)
  // 只是多一欄、馬上回同一頁,不寫「存檔中…」
  document.querySelectorAll("form[data-send-once]").forEach(function (form) {
    var buttons = Array.prototype.slice.call(form.querySelectorAll('button[type="submit"]'));
    var label = form.querySelector("[data-submit-label]");
    var status = form.querySelector("[data-send-status]");
    var labelText = label ? label.textContent : "";
    var sent = false;

    function ready() {
      sent = false;
      buttons.forEach(function (button) { button.disabled = false; });
      form.removeAttribute("aria-busy");
      if (label) label.textContent = labelText;
      if (status) status.textContent = "";
    }

    form.addEventListener("submit", function (event) {
      if (event.defaultPrevented) return;   // 被別的檢查擋下的不算送出
      if (sent) { event.preventDefault(); return; }
      sent = true;
      var quiet = Boolean(event.submitter && event.submitter.hasAttribute("data-send-quiet"));
      window.setTimeout(function () {
        if (!sent) return;
        buttons.forEach(function (button) { button.disabled = true; });
        form.setAttribute("aria-busy", "true");
        if (quiet) return;
        if (label) label.textContent = label.getAttribute("data-busy-label") || labelText;
        if (status) status.textContent = status.getAttribute("data-send-status") || "";
      }, 0);
    });

    // 重新整理或按「上一頁」回來時,有的瀏覽器會把按鈕「停用」的狀態一起還原:一載入就恢復成可以送出;
    // 整頁從 bfcache 回來(pageshow)也一樣
    ready();
    window.addEventListener("pageshow", function (event) {
      if (event.persisted) ready();
    });
  });

  // ---- 手機「選單」(<details>,沒有 JS 也能開關):按 Esc 或點選單外面就收起 ----
  var menu = document.querySelector("details.menu");
  if (menu) {
    document.addEventListener("keydown", function (event) {
      if (event.key === "Escape" && menu.open) {
        menu.open = false;
        menu.querySelector("summary").focus();
      }
    });
    document.addEventListener("click", function (event) {
      if (menu.open && !menu.contains(event.target)) menu.open = false;
    });
  }

  // ---- 上傳表單 ----
  var form = document.querySelector("[data-upload-form]");
  if (!form) return;

  var inputs = Array.prototype.slice.call(form.querySelectorAll('input[type="file"]'));
  var primaryInput = inputs[0];
  var chosen = form.querySelector("[data-file-chosen]");
  var chosenName = form.querySelector("[data-file-name]");
  var errorBox = form.querySelector("[data-file-error]");
  var errorText = form.querySelector("[data-file-error-text]");
  var submit = form.querySelector("[data-submit]");
  var submitLabel = form.querySelector("[data-submit-label]");
  var progress = form.querySelector("[data-progress]");
  var status = form.querySelector("[data-upload-status]");
  var wait = form.querySelector("[data-wait]");
  var waitSeconds = form.querySelector("[data-wait-seconds]");
  var maxBytes = parseInt(form.getAttribute("data-max-bytes"), 10) || 0;
  var submitText = submitLabel ? submitLabel.textContent : "";
  var timer = null;
  var previewUrl = null;

  function selectedFile() {
    for (var i = 0; i < inputs.length; i++) {
      if (inputs[i].files && inputs[i].files.length) return inputs[i].files[0];
    }
    return null;
  }

  function showError(message) {
    errorText.textContent = message;
    errorBox.hidden = false;
    primaryInput.setAttribute("aria-invalid", "true");
    var described = primaryInput.getAttribute("aria-describedby") || "";
    if (described.indexOf("file-error") === -1) {
      primaryInput.setAttribute("aria-describedby", (described + " file-error").trim());
    }
  }

  function clearError() {
    errorBox.hidden = true;
    errorText.textContent = "";
    primaryInput.removeAttribute("aria-invalid");
  }

  // 還沒選檔時「開始辨識」顯示成淡色(仍可按,按了會提示先拍照);選好檔案才變實心主要按鈕
  function setReady(ready) { form.setAttribute("data-ready", ready ? "true" : "false"); }
  setReady(Boolean(selectedFile()));

  function showChosen(file) {
    if (previewUrl) { URL.revokeObjectURL(previewUrl); previewUrl = null; }
    var old = chosen.querySelector("img");
    if (old) old.remove();
    setReady(Boolean(file));
    if (!file) { chosen.hidden = true; return; }
    chosenName.textContent = file.name;
    // 縮圖讓長輩確認「選對照片了」;需要 CSP img-src 允許 blob:
    if (file.type && file.type.indexOf("image/") === 0 && window.URL && URL.createObjectURL) {
      previewUrl = URL.createObjectURL(file);
      var img = document.createElement("img");
      img.className = "chosen__preview";
      img.alt = "剛剛選的照片縮圖";
      img.src = previewUrl;
      chosen.appendChild(img);
    }
    chosen.hidden = false;
  }

  inputs.forEach(function (input) {
    input.addEventListener("change", function () {
      // 兩個入口只留一個:選了相簿就清掉相機那個,反之亦然
      inputs.forEach(function (other) { if (other !== input) other.value = ""; });
      clearError();
      var file = input.files && input.files[0];
      if (file && maxBytes && file.size > maxBytes) {
        input.value = "";
        showChosen(null);
        showError("檔案超過 15MB。請改拍一張，或選較小的檔案。");
        return;
      }
      showChosen(file || null);
    });
  });

  // ---- 先選大類,再選這一類裡的哪一種 ----
  // 第二列預設只看得到五種類型(沒有 JS 也能選、能送出;各類的常見文件先藏著,伺服器各欄只收白名單值)。
  // 有 JS 時只留第一列那一類的名稱(大類選項的 data-kinds,空白分隔:身分證、稅單、保單…),
  // 標題換成「〔大類〕裡的哪一種?」;沒選大類就是五種類型。被藏起來的選項若正被選著,改選「不確定」,
  // 不送出畫面上看不到的選項
  var kindsGroup = form.querySelector("[data-kinds-group]");
  var kindsTitle = form.querySelector("[data-kinds-title]");
  var kindsUnsure = form.querySelector("[data-kinds-unsure]");
  var kindInputs = Array.prototype.slice.call(form.querySelectorAll('input[name="doc_type"]'));

  function showKinds() {
    var picked = form.querySelector('input[name="category"]:checked');
    if (!picked || !kindsGroup) return;
    var allowed = (picked.getAttribute("data-kinds") || "").split(" ").filter(Boolean);
    kindInputs.forEach(function (input) {
      input.closest(".chip").hidden = allowed.indexOf(input.value) === -1;
    });
    if (kindsTitle) kindsTitle.textContent = picked.getAttribute("data-kinds-legend") || "";
    var current = form.querySelector('input[name="doc_type"]:checked');
    if (kindsUnsure && (!current || current.closest(".chip").hidden)) {
      kindsUnsure.checked = true;
    }
  }

  form.querySelectorAll('input[name="category"]').forEach(function (input) {
    input.addEventListener("change", showKinds);
  });
  showKinds();

  function resetBusy() {
    if (timer) { window.clearInterval(timer); timer = null; }
    submit.disabled = false;
    if (submitLabel) submitLabel.textContent = submitText;
    form.removeAttribute("aria-busy");
    progress.hidden = true;
    wait.hidden = true;
    status.textContent = "";
  }

  form.addEventListener("submit", function (event) {
    if (!selectedFile()) {
      event.preventDefault();
      showError("請先拍照，或選一個檔案。");
      primaryInput.focus();
      return;
    }
    // 送出後才停用按鈕(停用的 input 不會被送出,所以只停用按鈕)
    submit.disabled = true;
    if (submitLabel) submitLabel.textContent = "辨識中…";
    form.setAttribute("aria-busy", "true");
    progress.hidden = false;
    status.textContent = "正在辨識，請不要關閉這個畫面。";
    // 等候秒數放在非 live 區,避免讀屏每秒唸一次
    var started = Date.now();
    wait.hidden = false;
    timer = window.setInterval(function () {
      waitSeconds.textContent = String(Math.floor((Date.now() - started) / 1000));
    }, 1000);
  });

  // 從結果頁按「上一頁」回來時(bfcache),把按鈕恢復成可按
  window.addEventListener("pageshow", function (event) {
    if (event.persisted) resetBusy();
  });

  // 伺服器回錯誤時,把焦點帶到欄位上,讓讀屏與鍵盤使用者直接看到錯誤
  if (!errorBox.hidden) primaryInput.focus();
})();

/* 這台裝置的偏好與設定頁(SET)。
   字級與朗讀速度只存在這個瀏覽器(localStorage),不送到伺服器,也不影響家裡其他人的手機;
   讀不到或存不了(無痕模式、瀏覽器擋掉)就用預設值。字級寫在 <html data-font>(app.css 依它放大整個版面),
   朗讀速度寫在 <html data-rate>(speech.js 唸的時候讀)。每一頁一載入就先套用,盡量不閃一下。 */
(function () {
  "use strict";

  var root = document.documentElement;
  // 可選的值與預設值;要和 web/render.py 的 DEVICE_PREFS、app.css 的 html[data-font] 一致
  var PREFS = {
    font: { values: ["standard", "large", "xlarge"], fallback: "standard" },
    rate: { values: ["slow", "standard"], fallback: "standard" }
  };
  var KEY = "pref-";   // localStorage 的鍵:pref-font、pref-rate

  function read(name) {
    var value = null;
    try { value = window.localStorage.getItem(KEY + name); } catch (e) { value = null; }
    return PREFS[name].values.indexOf(value) === -1 ? PREFS[name].fallback : value;
  }

  function save(name, value) {
    try { window.localStorage.setItem(KEY + name, value); return true; } catch (e) { return false; }
  }

  function applyAll() {
    Object.keys(PREFS).forEach(function (name) { root.setAttribute("data-" + name, read(name)); });
  }
  applyAll();

  // ---- 設定頁「這台裝置」:沒有 JS 時選項是停用的(只顯示預設),這裡才打開,勾上這台裝置存的值 ----
  var inputs = Array.prototype.slice.call(document.querySelectorAll("input[data-pref]"));
  var unsaved = document.querySelector("[data-pref-unsaved]");

  function syncInputs() {
    inputs.forEach(function (input) { input.checked = input.value === read(input.getAttribute("data-pref")); });
  }

  inputs.forEach(function (input) {
    input.disabled = false;
    input.addEventListener("change", function () {
      if (!input.checked) return;
      var name = input.getAttribute("data-pref");
      root.setAttribute("data-" + name, input.value);   // 改了馬上生效
      if (!save(name, input.value) && unsaved) unsaved.hidden = false;
    });
  });
  syncInputs();

  // 同一台裝置的別的分頁改了偏好:這一頁也跟著換
  window.addEventListener("storage", function (event) {
    if (event.key === null || event.key.indexOf(KEY) === 0) {
      applyAll();
      syncInputs();
    }
  });

  // ---- 加密 PDF 的密碼欄:「顯示密碼」(沒有 JS 時按鈕藏著,密碼一律遮住) ----
  // 長輩對著通知信一個字一個字打,打完想看一眼有沒有打錯:按一下顯示、再按一下遮回去
  document.querySelectorAll("[data-show-password]").forEach(function (button) {
    var input = document.getElementById(button.getAttribute("data-show-password"));
    var label = button.querySelector("span");
    if (!input || !label) return;
    button.hidden = false;
    button.addEventListener("click", function () {
      var show = input.type === "password";
      input.type = show ? "text" : "password";
      button.setAttribute("aria-pressed", show ? "true" : "false");
      label.textContent = show ? "隱藏密碼" : "顯示密碼";
      input.focus();
    });
  });

  // ---- 設定頁「系統設定」:從本機切到雲端備援要先問一次 ----
  // 確認框看按鈕上的 data-confirm…;依選的辨識模式換成帶確認的那顆「儲存設定」,
  // 另一顆藏起來並停用(在選項上按 Enter 送出時,也不會繞過確認框)
  var settingsForm = document.querySelector("form[data-settings-form]");
  var plainSave = settingsForm && settingsForm.querySelector("[data-save]");
  var confirmSave = settingsForm && settingsForm.querySelector("[data-save-confirm]");
  if (!plainSave || !confirmSave) return;

  function syncSave() {
    var picked = settingsForm.querySelector('input[name="provider"]:checked');
    var ask = Boolean(picked && picked.hasAttribute("data-needs-confirm"));
    plainSave.hidden = ask;
    plainSave.disabled = ask;
    confirmSave.hidden = !ask;
    confirmSave.disabled = !ask;
  }
  settingsForm.addEventListener("change", syncSave);
  syncSave();
})();
