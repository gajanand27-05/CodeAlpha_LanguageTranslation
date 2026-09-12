/* Front end for the translation tool.

   Talks to /api/translate and renders the answer. Nothing here decides how to
   translate; that all lives in translator.py. */

const input = document.getElementById("input");
const output = document.getElementById("output");
const sourceSel = document.getElementById("source");
const targetSel = document.getElementById("target");
const counter = document.getElementById("counter");
const note = document.getElementById("note");
const translateBtn = document.getElementById("translate");
const copyBtn = document.getElementById("copy");
const speakBtn = document.getElementById("speak");
const swapBtn = document.getElementById("swap");
const clearBtn = document.getElementById("clear");

const MAX = parseInt(input.getAttribute("maxlength"), 10);
let lastTranslation = "";
let busy = false;

function setNote(text, kind) {
  note.textContent = text || "";
  note.className = "note" + (kind ? " " + kind : "");
}

function updateCounter() {
  const n = input.value.length;
  counter.textContent = `${n} / ${MAX}`;
  counter.classList.toggle("near", n > MAX * 0.9);
}

function setOutput(text, isPlaceholder) {
  output.textContent = "";
  if (isPlaceholder) {
    const span = document.createElement("span");
    span.className = "placeholder";
    span.textContent = text;
    output.appendChild(span);
  } else {
    // textContent, not innerHTML: translated text is data from an external
    // service and must never be interpreted as markup.
    output.textContent = text;
  }
  const has = !isPlaceholder && text.trim().length > 0;
  copyBtn.disabled = !has;
  speakBtn.disabled = !has || !("speechSynthesis" in window);
  lastTranslation = has ? text : "";
}

async function doTranslate() {
  const text = input.value.trim();
  if (!text || busy) return;

  if (sourceSel.value === targetSel.value) {
    setNote("Source and target are the same language.", "warn");
    return;
  }

  busy = true;
  translateBtn.disabled = true;
  output.classList.add("busy");
  setNote("Translating...");

  try {
    const res = await fetch("/api/translate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text, source: sourceSel.value, target: targetSel.value }),
    });
    const data = await res.json();

    if (!data.ok) {
      setOutput("The translation appears here.", true);
      setNote(data.error, "error");
      return;
    }

    setOutput(data.text, false);

    // Say which service answered, and name the detected language when the
    // source was set to detect. Both are facts the user cannot otherwise see,
    // and the second is the only feedback that detection worked at all.
    const parts = [];
    if (data.detected_name) parts.push(`detected ${data.detected_name}`);
    parts.push(`via ${data.provider}`);
    if (data.unchanged) {
      setNote(`${parts.join(" · ")} · output matches the input`, "warn");
    } else {
      setNote(parts.join(" · "));
    }
  } catch (err) {
    setOutput("The translation appears here.", true);
    setNote(`Could not reach the server (${err.message}).`, "error");
  } finally {
    busy = false;
    translateBtn.disabled = false;
    output.classList.remove("busy");
  }
}

translateBtn.addEventListener("click", doTranslate);

// Ctrl+Enter translates, which is the shortcut people already expect in a box
// where plain Enter has to keep inserting a newline.
input.addEventListener("keydown", (e) => {
  if ((e.ctrlKey || e.metaKey) && e.key === "Enter") {
    e.preventDefault();
    doTranslate();
  }
});

input.addEventListener("input", updateCounter);

clearBtn.addEventListener("click", () => {
  input.value = "";
  updateCounter();
  setOutput("The translation appears here.", true);
  setNote("");
  input.focus();
});

swapBtn.addEventListener("click", () => {
  // "Detect language" cannot become a target, so a swap is only possible once
  // the source is a real language. Moving the translation into the input box
  // as well is what makes swapping useful: it translates straight back.
  if (sourceSel.value === "auto") {
    setNote("Pick a specific source language before swapping.", "warn");
    return;
  }
  const from = sourceSel.value;
  sourceSel.value = targetSel.value;
  targetSel.value = from;

  if (lastTranslation) {
    input.value = lastTranslation;
    updateCounter();
    setOutput("The translation appears here.", true);
    setNote("");
  }
});

copyBtn.addEventListener("click", async () => {
  if (!lastTranslation) return;
  try {
    await navigator.clipboard.writeText(lastTranslation);
    const original = copyBtn.textContent;
    copyBtn.textContent = "Copied";
    setTimeout(() => { copyBtn.textContent = original; }, 1200);
  } catch {
    setNote("The browser blocked clipboard access.", "warn");
  }
});

speakBtn.addEventListener("click", () => {
  if (!lastTranslation || !("speechSynthesis" in window)) return;
  // Browser speech synthesis rather than a server side audio file: no extra
  // dependency, no temporary mp3 files to write and clean up, and it starts
  // instantly. The tag tells the browser which voice to pick.
  window.speechSynthesis.cancel();
  const utterance = new SpeechSynthesisUtterance(lastTranslation);
  utterance.lang = targetSel.value;
  utterance.onerror = () => setNote("No speech voice installed for this language.", "warn");
  window.speechSynthesis.speak(utterance);
});

if (!("speechSynthesis" in window)) {
  speakBtn.title = "This browser has no speech synthesis";
}

updateCounter();
setOutput("The translation appears here.", true);
input.focus();
