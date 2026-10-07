/* 朗讀:用瀏覽器內建的 Web Speech API,而且只用這台裝置上的本機語音(不把文字送到雲端語音服務)。
   數字一律逐位唸(1854 → 「一 八 五 四」),金額、電話、發票號碼才不會聽錯。
   唸的內容是頁面上標了 data-speak-part 的文字(白話解說、待辦、藥袋聲明)。 */
(function (root) {
  "use strict";

  var DIGITS = "零一二三四五六七八九";

  /** 把文字裡的數字轉成逐位的中文數字;純函式,node 也能測。 */
  function spellDigits(text) {
    return String(text)
      // 全形數字 → 半形
      .replace(/[０-９]/g, function (c) { return String.fromCharCode(c.charCodeAt(0) - 0xFEE0); })
      // ISO 日期 2026-09-30 → 2026年9月30日(再逐位唸)
      .replace(/(\d{4})-(\d{1,2})-(\d{1,2})/g, function (m, y, mo, d) {
        return y + "年" + parseInt(mo, 10) + "月" + parseInt(d, 10) + "日";
      })
      // 千分位逗號 1,854 → 1854
      .replace(/(\d)[,,](?=\d{3}(?!\d))/g, "$1")
      // 小數點 12.5 → 12點5
      .replace(/(\d)\.(?=\d)/g, "$1點")
      .replace(/\d+/g, function (run) {
        return " " + run.split("").map(function (d) { return DIGITS[+d]; }).join(" ") + " ";
      })
      .replace(/ {2,}/g, " ")
      .trim();
  }

  /** 切成短句:Chrome 的長句朗讀約 15 秒會被截斷。
      (不用正規式的 lookbehind:iOS 16.4 以前的 Safari 會整支檔案解析失敗。) */
  function sentences(text) {
    var out = [];
    var buf = "";
    String(text).split("").forEach(function (ch) {
      buf += ch;
      if ("。!?;!?;\n".indexOf(ch) !== -1) {
        if (buf.trim()) out.push(buf.trim());
        buf = "";
      }
    });
    if (buf.trim()) out.push(buf.trim());
    return out;
  }

  /** 從瀏覽器的語音清單挑本機的中文語音;沒有就回 null,不改用雲端語音。純函式,node 也能測。
      雲端語音(例如 Chrome 的「Google 國語(臺灣)」)會把要唸的文字送出這台電腦,
      藥袋屬特種個資,所以寧可不唸也不送出(原則 6)。粵語(zh-HK)唸國語文件會聽不懂,不選。 */
  function pickLocalVoice(voices) {
    var local = (voices || []).filter(function (v) { return v && v.localService; });
    var lang = function (v) { return (v.lang || "").replace("_", "-").toLowerCase(); };
    return local.find(function (v) { return lang(v) === "zh-tw"; }) ||
      local.find(function (v) { return lang(v).indexOf("zh-hant") === 0; }) ||
      local.find(function (v) { return lang(v) === "zh-cn" || lang(v) === "zh"; }) || null;
  }

  root.ReadAloud = { spellDigits: spellDigits, sentences: sentences, pickLocalVoice: pickLocalVoice };

  if (typeof document === "undefined") return;  // node 測試環境到此為止

  var button = document.querySelector("[data-speak]");
  if (!button) return;
  var label = button.querySelector("[data-speak-label]");
  var status = document.querySelector("[data-speak-status]");
  var unsupported = document.querySelector("[data-speak-unsupported]");
  var synth = window.speechSynthesis;

  if (!synth || typeof window.SpeechSynthesisUtterance === "undefined") {
    if (unsupported) unsupported.hidden = false;
    return;
  }
  button.hidden = false;

  var idleText = label.textContent;
  var speaking = false;

  function setIdle(message) {
    speaking = false;
    label.textContent = idleText;
    if (status) status.textContent = message || "";
  }

  function speak() {
    var text = Array.prototype.map.call(
      document.querySelectorAll("[data-speak-part]"),
      function (el) { return el.textContent.replace(/\s+/g, " ").trim(); }
    ).filter(Boolean).join("。\n");
    var parts = sentences(text);
    if (!parts.length) return;
    // 語音清單在部分瀏覽器是頁面載入後才陸續出現;還沒出現時請長輩過幾秒再按
    var voices = synth.getVoices() || [];
    var voice = pickLocalVoice(voices);
    if (!voice) {
      if (status) {
        status.textContent = voices.length
          ? "這台裝置沒有內建的中文語音。為了不把文件內容傳到網路上，這裡不朗讀。"
          : "語音還在準備，請過幾秒再按一次。";
      }
      return;
    }

    synth.cancel();
    speaking = true;
    label.textContent = "停止朗讀";
    if (status) status.textContent = "開始朗讀";
    parts.forEach(function (part, i) {
      var u = new window.SpeechSynthesisUtterance(spellDigits(part));
      u.voice = voice;  // 一定指定本機語音:只設 lang 的話,瀏覽器可能自己挑到雲端語音
      u.lang = "zh-TW";  // 有些 Android 的 voice.lang 寫成 zh_TW,不直接沿用
      // 朗讀速度是這台裝置的偏好(設定頁;app.js 寫在 <html data-rate>):慢 0.8、標準 0.95
      u.rate = document.documentElement.getAttribute("data-rate") === "slow" ? 0.8 : 0.95;
      if (i === parts.length - 1) {
        u.onend = function () { if (speaking) setIdle("朗讀結束"); };
      }
      u.onerror = function () { if (speaking) setIdle("朗讀中斷了，可以再按一次"); };
      synth.speak(u);
    });
  }

  button.addEventListener("click", function () {
    if (speaking) {
      synth.cancel();
      setIdle("已停止朗讀");
    } else {
      speak();
    }
  });

  // 離開頁面時停止,避免換頁後還在唸
  window.addEventListener("pagehide", function () { synth.cancel(); });
})(typeof window !== "undefined" ? window : globalThis);
