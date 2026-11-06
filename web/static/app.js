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

  // ---- data-send-once:一張表單只送出一次(更正頁的「儲存更正並重新核對」) ----
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

  // ---- data-fill-target:更正頁「改成 QR/條碼上的值」 ----
  // 讀值和 QR 或帳單條碼不符時,欄位下面寫著機讀碼上的值;有 JS 才露出「改成…」(沒有 JS 就照著打)。
  // 按了只把值填進那一欄、焦點移過去,不送出:家人還是要按「儲存更正並重新核對」才生效。
  // 欄位已經是這個值時按鈕停用並寫「已改成…」;家人又改掉就恢復成可以按
  document.querySelectorAll("[data-fill-wrap]").forEach(function (wrap) {
    var button = wrap.querySelector("button[data-fill-target]");
    var input = button && document.getElementById(button.getAttribute("data-fill-target"));
    if (!input) return;
    var value = button.getAttribute("data-fill-value") || "";
    var label = button.querySelector("[data-fill-label]");
    var labelText = label ? label.textContent : "";

    function sync() {
      var filled = input.value === value;
      button.disabled = filled;
      if (label) label.textContent = filled ? button.getAttribute("data-fill-done") || labelText : labelText;
    }

    wrap.hidden = false;
    button.addEventListener("click", function () {
      input.value = value;
      input.focus();
      sync();
    });
    input.addEventListener("input", sync);
    sync();
  });

  // ---- 更正頁:原件放大、跳到下一個問題(參考 Rossum 的核對畫面,11/4) ----
  // 放大:沒有 JS 時按「放大」(藏起來的勾選框)就放大,框裡可以捲動(app.css 的 .zoom);這裡再加上按圖片本身
  // 放大或縮回,放大時捲到按的那一點(用「放大」按的捲到中間)。在「放大」上按 Enter 也算:勾選框原本只認
  // 空白鍵,Enter 在表單裡還會變成送出
  document.querySelectorAll("[data-zoom]").forEach(function (zoom) {
    var check = zoom.querySelector(".zoom__check");
    var frame = zoom.querySelector("[data-zoom-frame]");
    var img = frame && frame.querySelector("img");
    if (!check || !img) return;
    zoom.classList.add("zoom--js");

    // x、y 是照片上的比例(0–1):放大後把那一點捲到框的中間
    function centerOn(x, y) {
      frame.scrollLeft = x * img.offsetWidth - frame.clientWidth / 2;
      frame.scrollTop = y * img.offsetHeight - frame.clientHeight / 2;
    }

    // 按的位置在照片上的比例;沒放大時照片照比例縮在框裡(object-fit: contain),要扣掉上下或左右的留白
    function pointOf(event) {
      var box = img.getBoundingClientRect();
      if (!img.naturalWidth || !img.naturalHeight || !box.width || !box.height) return [0.5, 0.5];
      var scale = Math.min(box.width / img.naturalWidth, box.height / img.naturalHeight);
      var w = img.naturalWidth * scale, h = img.naturalHeight * scale;
      var x = (event.clientX - box.left - (box.width - w) / 2) / w;
      var y = (event.clientY - box.top - (box.height - h) / 2) / h;
      return [Math.min(Math.max(x, 0), 1), Math.min(Math.max(y, 0), 1)];
    }

    img.addEventListener("click", function (event) {
      var point = check.checked ? null : pointOf(event);
      check.checked = !check.checked;
      if (point) centerOn(point[0], point[1]);
    });
    check.addEventListener("change", function () {
      if (check.checked) centerOn(0.5, 0.5);
    });
    check.addEventListener("keydown", function (event) {
      if (event.key !== "Enter") return;
      event.preventDefault();
      check.click();
    });
  });

  // ---- 更正頁:一次一個問題(使用者 11/5) ----
  // 沒有 JS 時有問題的欄位全部展開(<details open>,按摘要也能收起來)。有 JS 時只留一個:伺服器要焦點去的那一欄、
  // 填錯的那一欄,或第一欄;其他收成一行摘要(欄位名稱加原因),按了才展開,原本打開的那一個收起來,焦點放進去。
  // 收起來只是看不到,欄位還在同一張表單裡,送出時照樣送。原因說的是存好的讀值,改了還沒存不會變,
  // 所以收起來時摘要旁寫「已修改，尚未儲存」;寫錯的寫「要修改」,不會因為收起來就沒人看到
  var folds = Array.prototype.slice.call(document.querySelectorAll("details[data-issue-fold]"));

  function differsFromPage(field) {
    if (field.type === "checkbox" || field.type === "radio") return field.checked !== field.defaultChecked;
    if (field.tagName === "SELECT") {
      return Array.prototype.some.call(field.options, function (option) { return option.selected !== option.defaultSelected; });
    }
    return field.value !== field.defaultValue;
  }

  function collapse(fold) {
    fold.open = false;
    var state = fold.querySelector("[data-fold-state]");
    if (!state) return;
    var bad = Boolean(fold.querySelector('[aria-invalid="true"]'));
    var changed = Array.prototype.some.call(fold.querySelectorAll("input[name], select[name], textarea[name]"), differsFromPage);
    state.textContent = bad ? "要修改" : changed ? "已修改，尚未儲存" : "";
    state.hidden = !bad && !changed;
    state.classList.toggle("issue-fold__state--bad", bad);
  }

  function expand(fold) {
    folds.forEach(function (other) {
      if (other !== fold && other.open) collapse(other);
    });
    fold.open = true;
  }

  // 展開的那一欄不在畫面上半部才捲過去(手機鍵盤跳出來時還看得到);頂部品牌列的高度由 scroll-padding-top 留著
  function reveal(fold) {
    var pad = parseFloat(window.getComputedStyle(document.documentElement).scrollPaddingTop) || 0;
    var top = fold.getBoundingClientRect().top;
    if (top < pad || top > window.innerHeight / 2) fold.scrollIntoView({block: "start"});
  }

  if (folds.length) {
    expand(folds.filter(function (fold) { return fold.querySelector("[autofocus]"); })[0]
      || folds.filter(function (fold) { return fold.querySelector('[aria-invalid="true"]'); })[0] || folds[0]);
  }

  folds.forEach(function (fold) {
    var head = fold.querySelector("summary");
    if (!head) return;
    head.addEventListener("click", function (event) {
      event.preventDefault();
      if (fold.open) {
        collapse(fold);
        return;
      }
      expand(fold);
      reveal(fold);
      var field = document.getElementById(fold.getAttribute("data-focus"));
      if (field) field.focus({preventScroll: true});
    });
  });

  // 「下一個問題」:沒有 JS 時是頁內連結(捲到那一欄);這裡把目前這個收起來、展開下一個,焦點放進去,讀屏會念出欄名與原因。
  // 最後一個(下一步)全部收起來,焦點到儲存按鈕:改了什麼在摘要上看得到
  document.querySelectorAll("a[data-next-issue]").forEach(function (link) {
    link.addEventListener("click", function (event) {
      var target = document.getElementById(link.getAttribute("data-next-issue"));
      if (!target) return;
      event.preventDefault();
      var row = document.getElementById((link.getAttribute("href") || "").replace(/^#/, ""));
      if (row && row.hasAttribute("data-issue-fold")) {
        expand(row);
        reveal(row);
      } else {
        folds.forEach(function (fold) { collapse(fold); });
        target.scrollIntoView({block: "center"});
      }
      target.focus({preventScroll: true});
    });
  });

  // ---- data-warn-unsaved:改了還沒存就要離開時,瀏覽器先問一次(web-design-guidelines,11/5) ----
  // 更正頁、對獎、系統設定:填到一半按到別的分頁、上一頁或關掉分頁,填的字會不見。只和頁面載入時的值比
  // (differsFromPage),改回原樣就不問;真的送出就不問,送出被擋下(還在這一頁)照樣問。按「上一頁」回來又會問。
  // 瀏覽器只顯示它自己的固定句子,網頁寫的字不會出現
  document.querySelectorAll("form[data-warn-unsaved]").forEach(function (form) {
    var leaving = false;
    form.addEventListener("submit", function (event) {
      leaving = true;
      window.setTimeout(function () { if (event.defaultPrevented) leaving = false; }, 0);
    });
    window.addEventListener("beforeunload", function (event) {
      if (leaving) return;
      if (!Array.prototype.some.call(form.querySelectorAll("input[name], select[name], textarea[name]"), differsFromPage)) return;
      event.preventDefault();
      event.returnValue = "";
    });
    window.addEventListener("pageshow", function (event) {
      if (event.persisted) leaving = false;
    });
  });

  // ---- data-share:結果頁「請家人看一下」 ----
  // 用手機內建的分享選單傳這一頁的網址,長輩自己選 LINE 或簡訊;只有網址與固定的一句話,不含文件內容,對方要登入才看得到。
  // 不支援分享的瀏覽器改成複製連結;兩者都不行就不顯示按鈕。長輩按了取消就什麼都不說。系統不會通知家人
  document.querySelectorAll("button[data-share]").forEach(function (button) {
    var status = document.querySelector("[data-share-status]");
    var canShare = typeof navigator.share === "function";
    var canCopy = !!(navigator.clipboard && navigator.clipboard.writeText);
    if (!canShare && !canCopy) return;
    var url = location.origin + location.pathname;   // 不帶 ?msg= 之類的參數
    var text = button.getAttribute("data-share-text") || "";

    function say(message) { if (status) status.textContent = message; }
    function copy() {
      if (!canCopy) { say("這個瀏覽器不能分享，請把這一頁的網址傳給家人。"); return; }
      navigator.clipboard.writeText(text + " " + url).then(function () {
        say("已複製連結，請貼給家人。");
      }, function () {
        say("沒有複製成功，請把這一頁的網址傳給家人。");
      });
    }

    button.hidden = false;
    button.addEventListener("click", function () {
      say("");
      if (!canShare) { copy(); return; }
      navigator.share({ title: "看有", text: text, url: url }).catch(function (error) {
        if (error && error.name === "AbortError") return;
        copy();
      });
    });
  });

  // ---- 上傳後的進度頁:每秒問伺服器做到哪一步,做完就打開結果 ----
  // 每一步是伺服器真的做完才亮(狀態與文字都照伺服器回的換,這裡不自己推進)。沒有 JS 時是 <noscript> 裡的
  // meta refresh 每 3 秒重新整理。問不到(網路斷了、服務重開、處理失敗)就改成整頁重新整理,由伺服器的頁面說明
  var jobBox = document.querySelector("[data-job-status]");
  if (jobBox) {
    var jobUrl = jobBox.getAttribute("data-job-status");
    var jobSay = jobBox.querySelector("[data-job-say]");
    var reloadSoon = function () {
      window.setTimeout(function () { window.location.reload(); }, 3000);
    };
    var showStep = function (step) {
      var item = jobBox.querySelector('[data-step="' + step.id + '"]');
      if (!item) return;
      item.className = "step step--" + step.state;
      item.querySelector("[data-step-state]").textContent = step.state_text;
      item.querySelector("[data-step-hint]").textContent = step.hint;
      item.querySelector("[data-step-time]").textContent = step.time;
    };
    var poll = function () {
      window.fetch(jobUrl, { credentials: "same-origin", cache: "no-store", headers: { Accept: "application/json" } })
        .then(function (response) {
          if (!response.ok) throw new Error(String(response.status));
          return response.json();
        })
        .then(function (status) {
          if (status.next) {
            window.location.replace(status.next);
            return;
          }
          status.steps.forEach(showStep);
          if (jobSay && jobSay.textContent !== status.say) jobSay.textContent = status.say;
          window.setTimeout(poll, 1000);
        })
        .catch(reloadSoon);
    };
    if (window.fetch) window.setTimeout(poll, 1000);
    else reloadSoon();   // 太舊的瀏覽器沒有 fetch:和沒有 JS 一樣,整頁重新整理
  }

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

  // ---- 標題旁的 ⓘ「說明」(<details class="note">,沒有 JS 也能按圖示開關):內容浮在圖示下面的小框 ----
  // 使用者 11/5:「用彈出視窗呈現 加一點過度動畫」。這裡接手開關:一次只開一個;點小框外面、焦點移到外面、按 Esc、
  // 再按一次圖示都會收起。收起時先加 data-closing 讓小框淡出(app.css),動畫跑完才真的關;減少動態或不支援動畫時
  // 等不到 animationend,最多 0.2 秒照樣關。打開時小框超出畫面左右就挪回來(--note-shift),尖角照樣指著圖示。
  // 點外面同時聽 pointerdown 與 click:iOS 的 Safari 點在不能按的字上不一定送 click
  var notes = Array.prototype.slice.call(document.querySelectorAll("details.note"));
  if (notes.length) {
    var NOTE_EDGE = 16;   // 小框離畫面左右至少留的 px(= app.css 小框最寬 100vw - 2rem 的一半,手機上左右一樣寬)
    // 用排版的位置算(offsetLeft、offsetWidth):淡入時的縮放和已經挪過的 --note-shift 都不算進去,量到的就是框本來的寬
    var placeNote = function (note) {
      var body = note.querySelector(".note__body");
      if (!body) return;
      var left = note.getBoundingClientRect().left + body.offsetLeft;
      var right = left + body.offsetWidth;
      var limit = document.documentElement.clientWidth - NOTE_EDGE;
      var shift = right > limit ? limit - right : 0;
      if (left + shift < NOTE_EDGE) shift = NOTE_EDGE - left;
      body.style.setProperty("--note-shift", Math.round(shift) + "px");
    };
    var closeNote = function (note, focusBack) {
      if (!note.open || note.hasAttribute("data-closing")) return;
      note.setAttribute("data-closing", "");
      var done = function () {
        if (!note.hasAttribute("data-closing")) return;   // 淡出途中又被打開了
        note.open = false;
        note.removeAttribute("data-closing");
        if (focusBack) note.querySelector("summary").focus();
      };
      var body = note.querySelector(".note__body");
      if (body) body.addEventListener("animationend", done, { once: true });
      window.setTimeout(done, 200);
    };
    var openNote = function (note) {
      notes.forEach(function (other) { if (other !== note) closeNote(other, false); });
      note.removeAttribute("data-closing");
      note.open = true;
      placeNote(note);   // 同一輪裡算好位置:畫面不會先閃一下超出去的樣子
    };
    notes.forEach(function (note) {
      note.querySelector("summary").addEventListener("click", function (event) {
        event.preventDefault();
        if (note.open && !note.hasAttribute("data-closing")) closeNote(note, false);
        else openNote(note);
      });
    });
    var closeOutside = function (event) {
      notes.forEach(function (note) {
        if (note.open && !note.contains(event.target)) closeNote(note, false);
      });
    };
    document.addEventListener("pointerdown", closeOutside);
    document.addEventListener("click", closeOutside);
    document.addEventListener("focusin", closeOutside);
    document.addEventListener("keydown", function (event) {
      if (event.key !== "Escape") return;
      notes.forEach(function (note) { if (note.open) closeNote(note, true); });
    });
    window.addEventListener("resize", function () {
      notes.forEach(function (note) { if (note.open && !note.hasAttribute("data-closing")) placeNote(note); });
    });
  }

  // ---- data-collapse-narrow:手冊文章的「這一篇的內容」(Impeccable 稽核,11/5) ----
  // 電腦上一直打開,摘要只是標題:按了不收,Tab 也不停在上面。手機與平板先收起來,按「這一篇的內容」才打開,
  // 免得十幾個連結排在內文前面。轉向或改視窗寬度時跟著換;沒有 JS(或沒有 matchMedia)時一直打開
  document.querySelectorAll("details[data-collapse-narrow]").forEach(function (fold) {
    if (!window.matchMedia) return;
    var narrow = window.matchMedia("(max-width: 63.99rem)");
    var summary = fold.querySelector("summary");
    var apply = function () {
      fold.open = !narrow.matches;
      if (summary) summary.tabIndex = narrow.matches ? 0 : -1;
    };
    if (summary) {
      summary.addEventListener("click", function (event) {
        if (!narrow.matches) event.preventDefault();
      });
    }
    apply();
    if (narrow.addEventListener) narrow.addEventListener("change", apply);
  });

  // ---- 上傳表單 ----
  var form = document.querySelector("[data-upload-form]");
  if (!form) return;

  var inputs = Array.prototype.slice.call(form.querySelectorAll('input[type="file"]'));
  var primaryInput = inputs[0];
  var pickInput = inputs[inputs.length - 1];     // 「選照片」;拖進來的照片也放在這一欄
  var chosen = form.querySelector("[data-file-chosen]");
  var chosenName = form.querySelector("[data-file-name]");
  var chosenPhoto = form.querySelector("[data-file-photo]") || chosen;
  var reset = form.querySelector("[data-file-reset]");
  var errorBox = form.querySelector("[data-file-error]");
  var errorText = form.querySelector("[data-file-error-text]");
  var submit = form.querySelector("[data-submit]");
  var submitLabel = form.querySelector("[data-submit-label]");
  var progress = form.querySelector("[data-progress]");
  var progressBar = form.querySelector("[data-progress-bar]");
  var status = form.querySelector("[data-upload-status]");
  var wait = form.querySelector("[data-wait]");
  var waitSeconds = form.querySelector("[data-wait-seconds]");
  var waitPercent = form.querySelector("[data-wait-percent]");
  var zone = form.querySelector("[data-drop-zone]");
  var hint = form.querySelector("[data-photo-hint]");
  var checkUrl = form.getAttribute("data-check-url");
  var csrf = form.querySelector('input[name="csrf_token"]');
  var maxBytes = parseInt(form.getAttribute("data-max-bytes"), 10) || 0;
  var submitText = submitLabel ? submitLabel.textContent : "";
  var timer = null;
  var previewUrl = null;
  var sending = false;

  // 拖進來的檔不受 <input accept> 限制,先照伺服器的白名單(supported_extensions)擋一次,錯誤文字同 MSG_BAD_EXT;
  // 伺服器照樣再檢查一次
  var ACCEPTED_NAME = /\.(jpe?g|png|webp|bmp|tiff?|pdf)$/i;
  var MSG_BAD_EXT = "只能上傳照片（JPG、PNG、WEBP、BMP、TIFF）或 PDF。";
  var MSG_ONE_ONLY = "一次只能放一張，請只拖一張進來。";
  var MSG_NO_DROP = "這個瀏覽器不能把照片拖進來，請按「選照片」。";
  var MSG_NETWORK = "上傳沒有成功。請檢查網路，再按一次「開始辨識」；還是不行就重新整理這一頁。";

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

  // ---- 拍照檢查:縮圖出來後,把縮小的照片送回家裡這台電腦問一次「模糊或缺角嗎」(POST /upload/check) ----
  // 只有偵測到才寫一行提示;問不到、太大、解不開都什麼都不寫。提示不擋人,「開始辨識」一直能按,也不等這裡。
  // canvas 重新編碼後沒有拍攝地點這類中繼資料;伺服器只在記憶體裡算,不存檔、不留紀錄、不送模型(src/photo_check.py)。
  // CHECK_EDGE 要和 src/photo_check.py 一樣(門檻是照這個大小量的);CHECK_MAX_BYTES 要在伺服器的表單上限(1MB)內
  var CHECK_EDGE = 1600, CHECK_QUALITY = 0.8, CHECK_MAX_BYTES = 1000 * 1024;
  var checkId = 0;     // 換一張、開始上傳就加一,還沒回來的答案不算數

  function clearHint() {
    checkId += 1;
    if (hint) hint.textContent = "";
  }

  function askServer(blob, id) {
    var data = new window.FormData();
    data.append(csrf.name, csrf.value);
    data.append("photo", blob, "check.jpg");
    window.fetch(checkUrl, { method: "POST", body: data, credentials: "same-origin", headers: { Accept: "application/json" } })
      .then(function (response) { return response.ok ? response.json() : null; })
      .then(function (answer) {
        if (id === checkId && answer && typeof answer.hint === "string") hint.textContent = answer.hint;
      })
      .catch(function () {});     // 問不到就當作沒偵測到
  }

  function checkPhoto(img) {
    if (!hint || !checkUrl || !csrf || !window.fetch || !window.FormData || !img.naturalWidth) return;
    var scale = Math.min(1, CHECK_EDGE / Math.max(img.naturalWidth, img.naturalHeight));
    var canvas = document.createElement("canvas");
    canvas.width = Math.round(img.naturalWidth * scale);
    canvas.height = Math.round(img.naturalHeight * scale);
    var context = canvas.getContext && canvas.getContext("2d");
    if (!context || !canvas.toBlob) return;
    context.imageSmoothingQuality = "high";
    context.drawImage(img, 0, 0, canvas.width, canvas.height);
    var id = checkId;
    canvas.toBlob(function (blob) {
      canvas.width = 0;       // 手機的 canvas 記憶體有限,用完馬上放掉
      if (id === checkId && blob && blob.size <= CHECK_MAX_BYTES) askServer(blob, id);
    }, "image/jpeg", CHECK_QUALITY);
  }

  // 一次一個主要按鈕(使用者 11/4):還沒選檔只看得到「拍照讀文件」「選照片」;選好後 data-ready="true",
  // 兩顆按鈕收起來,換成縮圖、「換一張」、類型與「開始辨識」(app.css 依 data-ready 顯示)
  function setReady(ready) { form.setAttribute("data-ready", ready ? "true" : "false"); }

  function showChosen(file) {
    clearHint();
    if (previewUrl) { URL.revokeObjectURL(previewUrl); previewUrl = null; }
    var old = chosenPhoto.querySelector("img");
    if (old) old.remove();
    setReady(Boolean(file));
    if (!file) { chosen.hidden = true; return; }
    chosenName.textContent = file.name;
    // 縮圖讓長輩確認「選對照片了」;需要 CSP img-src 允許 blob:。PDF 和瀏覽器解不開的圖沒有縮圖,也不檢查
    if (file.type && file.type.indexOf("image/") === 0 && window.URL && URL.createObjectURL) {
      previewUrl = URL.createObjectURL(file);
      var img = document.createElement("img");
      img.className = "chosen__preview";
      img.alt = "剛剛選的照片縮圖";
      img.addEventListener("load", function () { checkPhoto(img); });
      img.src = previewUrl;
      chosenPhoto.appendChild(img);
    }
    chosen.hidden = false;
  }
  showChosen(selectedFile());     // 重新整理後,有的瀏覽器會留住選好的檔

  // 選好一個檔:相機、相簿與拖進來的都放在 input 裡,沒有進度條時一般送出也帶得到
  function usePicked(input) {
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
    // 兩顆按鈕(連同剛按的那個欄位)收起來了,焦點移到「開始辨識」
    if (file) submit.focus();
  }

  inputs.forEach(function (input) {
    input.addEventListener("change", function () { usePicked(input); });
  });

  // 「換一張」:清掉選好的檔,回到兩顆按鈕;選的類型留著
  if (reset) {
    reset.addEventListener("click", function () {
      inputs.forEach(function (input) { input.value = ""; });
      clearError();
      showChosen(null);
      primaryInput.focus();
    });
  }

  // ---- 把照片拖進虛線框(電腦):放開的效果和按「選照片」一樣 ----
  // 拖著檔案進框時框變色(data-drag)。經過框裡的每個元素都會各進出一次,所以數進出的次數,回到 0 才是真的離開。
  // 沒放準(放在框外)時瀏覽器會直接打開那張照片、離開這一頁:整頁都先攔下來,只有框裡才算數。
  // 一次一張:拖了好幾張或格式不對就照實說,原本選好的照片不動
  var dragDepth = 0;

  function draggingFiles(event) {
    var types = event.dataTransfer && event.dataTransfer.types;
    return Boolean(types) && Array.prototype.indexOf.call(types, "Files") !== -1;
  }

  function setDragging(on) {
    if (on) zone.setAttribute("data-drag", "true");
    else zone.removeAttribute("data-drag");
  }

  function useDropped(files) {
    if (!files || !files.length) return;
    clearError();
    if (files.length > 1) { showError(MSG_ONE_ONLY); return; }
    if (!ACCEPTED_NAME.test(files[0].name)) { showError(MSG_BAD_EXT); return; }
    try { pickInput.files = files; } catch (e) { /* 舊瀏覽器不能指定,下面照實說 */ }
    if (!pickInput.files || pickInput.files[0] !== files[0]) { showError(MSG_NO_DROP); return; }
    usePicked(pickInput);
  }

  if (zone) {
    zone.addEventListener("dragenter", function (event) {
      if (!draggingFiles(event) || sending) return;
      dragDepth += 1;
      setDragging(true);
    });
    zone.addEventListener("dragleave", function (event) {
      if (!draggingFiles(event)) return;
      dragDepth = Math.max(0, dragDepth - 1);
      if (!dragDepth) setDragging(false);
    });
    window.addEventListener("dragover", function (event) {
      if (!draggingFiles(event)) return;
      event.preventDefault();
      event.dataTransfer.dropEffect = !sending && zone.contains(event.target) ? "copy" : "none";
    });
    window.addEventListener("drop", function (event) {
      if (!draggingFiles(event)) return;
      event.preventDefault();
      dragDepth = 0;
      setDragging(false);
      if (!sending && zone.contains(event.target)) useDropped(event.dataTransfer.files);
    });
  }

  // ---- 上傳與進度:照實際傳了多少畫進度條,傳完照伺服器的轉址走 ----
  function showProgress(fraction) {
    var percent = Math.round(fraction * 100);
    progressBar.style.width = percent + "%";
    progress.setAttribute("aria-valuenow", String(percent));
    if (waitPercent) waitPercent.textContent = String(percent);
  }

  function resetBusy() {
    if (timer) { window.clearInterval(timer); timer = null; }
    sending = false;
    submit.disabled = false;
    if (reset) reset.disabled = false;
    if (submitLabel) submitLabel.textContent = submitText;
    form.removeAttribute("aria-busy");
    progress.hidden = true;
    if (progressBar) showProgress(0);
    wait.hidden = true;
    status.textContent = "";
  }

  // 伺服器轉址了(進度頁、結果頁、PDF 密碼頁)就照著走。沒轉址的是 POST /upload 自己回的頁面(太大、格式不對、
  // 處理失敗…):整頁換成伺服器寫的那一頁,和沒有 JS 時送出看到的一樣,錯誤文字與下一步都沿用
  function uploaded(xhr) {
    var url = xhr.responseURL || "";
    if (url && !/\/upload$/.test(url.split(/[?#]/)[0])) {
      window.location.assign(url);
      return;
    }
    if (timer) { window.clearInterval(timer); timer = null; }
    document.open();
    document.write(xhr.responseText);
    document.close();
  }

  function sendWithProgress() {
    var xhr = new window.XMLHttpRequest();
    xhr.upload.addEventListener("progress", function (event) {
      if (event.lengthComputable && event.total) showProgress(event.loaded / event.total);
    });
    xhr.upload.addEventListener("load", function () {
      showProgress(1);
      if (submitLabel) submitLabel.textContent = "請稍等…";
      status.textContent = "照片傳好了，請稍等。";
    });
    xhr.addEventListener("load", function () { uploaded(xhr); });
    xhr.addEventListener("error", function () {
      resetBusy();
      showError(MSG_NETWORK);
      submit.focus();
    });
    xhr.open("POST", form.getAttribute("action"));
    xhr.send(new window.FormData(form));
  }

  form.addEventListener("submit", function (event) {
    if (sending) { event.preventDefault(); return; }
    if (!selectedFile()) {
      event.preventDefault();
      showError("請先拍照，或選一個檔案。");
      primaryInput.focus();
      return;
    }
    checkId += 1;    // 按下去就不再等拍照檢查;已經寫出來的提示留著
    clearError();
    // 送出後才停用按鈕(停用的 input 不會被送出,所以只停用按鈕)
    sending = true;
    submit.disabled = true;
    if (reset) reset.disabled = true;
    if (submitLabel) submitLabel.textContent = "上傳中…";
    form.setAttribute("aria-busy", "true");
    status.textContent = "正在上傳，請不要關閉這個畫面。";
    // 看不到進度的舊瀏覽器照舊用一般表單送出(沒有 JS 時也是這樣送)
    if (!progressBar || !window.XMLHttpRequest || !window.FormData) return;
    event.preventDefault();
    progress.hidden = false;
    showProgress(0);
    // 百分比與等候秒數放在非 live 區,避免讀屏一直唸
    var started = Date.now();
    wait.hidden = false;
    waitSeconds.textContent = "0";
    timer = window.setInterval(function () {
      waitSeconds.textContent = String(Math.floor((Date.now() - started) / 1000));
    }, 1000);
    sendWithProgress();
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

  // ---- 密碼欄的眼睛:按一下顯示、再按一下遮回去(沒有 JS 時按鈕藏著,密碼一律遮住) ----
  // 長輩對著通知信一個字一個字打,打完想看一眼有沒有打錯;按鈕的名稱照狀態寫(讀屏念「顯示密碼」或「隱藏密碼」)
  document.querySelectorAll("[data-show-password]").forEach(function (button) {
    var input = document.getElementById(button.getAttribute("data-show-password"));
    if (!input) return;
    button.hidden = false;
    button.addEventListener("click", function () {
      var show = input.type === "password";
      input.type = show ? "text" : "password";
      button.setAttribute("aria-pressed", show ? "true" : "false");
      button.setAttribute("aria-label", show ? "隱藏密碼" : "顯示密碼");
      input.focus();
    });
  });

  // ---- 表單:離開欄位才檢查(使用者 11/4 的表單規範第 11 點) ----
  // 有 data-error 的輸入框(樣板 _field.html):離開欄位時用瀏覽器內建的檢查(required、pattern、type=email、min、max),
  // 不對就在框的正下方寫 data-error 的說明(寫清楚怎麼改);邊打字不報錯,已經報錯的改對了就馬上收掉。
  // 日期只填了一半(瀏覽器的 badInput):另外說「沒有填完整」;選填的日期也要說,不然送出時瀏覽器送空字串,原本的值會被清掉,
  // 所以送出前也攔一次,把焦點帶到那一欄。伺服器照樣再檢查一次,這裡只是早一點告訴使用者
  var INCOMPLETE = "日期沒有填完整，請把年、月、日都填好。";
  function incomplete(input) { return Boolean(input.validity && input.validity.badInput); }
  document.querySelectorAll(".ff__input[data-error]").forEach(function (input) {
    var field = input.closest(".ff");
    var box = field && field.querySelector("[data-ff-error]");
    var text = field && field.querySelector("[data-ff-error-text]");
    if (!box || !text) return;
    function mark(bad) {
      box.hidden = !bad;
      if (bad) text.textContent = incomplete(input) ? INCOMPLETE : input.getAttribute("data-error");
      if (bad) input.setAttribute("aria-invalid", "true");
      else input.removeAttribute("aria-invalid");
    }
    input.addEventListener("blur", function () {
      if (input.value === "" && !input.required && !incomplete(input)) { mark(false); return; }
      mark(!input.checkValidity());
    });
    input.addEventListener("input", function () {
      if (!box.hidden && input.checkValidity()) mark(false);
    });
    if (input.form) {
      input.form.addEventListener("submit", function (event) {
        if (!incomplete(input)) return;
        mark(true);
        event.preventDefault();
        input.focus();
      });
    }
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
