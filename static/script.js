const setupScreen = document.getElementById("setup-screen");
const artistInput = document.getElementById("artist-input");
const startBtn = document.getElementById("start-btn");
const nextBtn = document.getElementById("next-btn");
const quizSection = document.getElementById("quiz");
const scoreSection = document.getElementById("quiz-topbar");
const quizHomeBtn = document.getElementById("quiz-home-btn");
const snippetEl = document.getElementById("snippet");
const choicesEl = document.getElementById("choices");
const feedbackEl = document.getElementById("feedback");
const statusEl = document.getElementById("status");
const scoreCorrectEl = document.getElementById("score-correct");
const scoreTotalEl = document.getElementById("score-total");
const scoreArtistEl = document.getElementById("score-artist");
const progressEl = document.getElementById("progress");
const progressBarFillEl = document.getElementById("progress-bar-fill");
const suggestionsEl = document.getElementById("suggestions");
const artistConfirmedIcon = document.getElementById("artist-confirmed-icon");
const artistClearBtn = document.getElementById("artist-clear-btn");
const countButtons = Array.from(document.querySelectorAll(".count-btn"));
const difficultyButtons = Array.from(document.querySelectorAll(".difficulty-btn"));
const scopeButtons = Array.from(document.querySelectorAll(".scope-btn"));
const resultsScreen = document.getElementById("results");
const resultsScoreEl = document.getElementById("results-score");
const resultsReviewEl = document.getElementById("results-review");
const playAgainBtn = document.getElementById("play-again-btn");
const homeBtn = document.getElementById("home-btn");
const loadingEl = document.getElementById("loading");
const loadingTextEl = document.getElementById("loading-text");
const cancelLoadBtn = document.getElementById("cancel-load-btn");
const loadingProgressBarEl = document.getElementById("loading-progress-bar");
const loadingProgressFillEl = document.getElementById("loading-progress-fill");

let selectedCount = "10";
let selectedDifficulty = "normal";
let selectedScope = "top25";
let questions = [];
let questionIndex = 0;
let score = { correct: 0, total: 0 };
let reviewLog = [];
let lastArtist = "";
let lastCount = "10";
let lastDifficulty = "normal";
let lastScope = "top25";

let suggestAbortController = null;
let suggestRequestId = 0;
let suggestDebounceTimer = null;
let activeSuggestionIndex = -1;
let artistConfirmed = false;

function setArtistConfirmed(value) {
  artistConfirmed = value;
  startBtn.disabled = !value;
  artistInput.classList.toggle("confirmed", value);
  artistConfirmedIcon.classList.toggle("hidden", !value);
  if (value) {
    // Once confirmed, the suggestion list has served its purpose -- keep it closed
    // even if the input is refocused later.
    suggestionsEl.classList.add("hidden");
  }
}

setArtistConfirmed(false);

function setSetupControlsDisabled(disabled) {
  artistInput.disabled = disabled;
  artistClearBtn.disabled = disabled;
  [...countButtons, ...difficultyButtons, ...scopeButtons].forEach((btn) => {
    btn.disabled = disabled;
  });
}

countButtons.forEach((btn) => {
  btn.addEventListener("click", () => {
    countButtons.forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");
    selectedCount = btn.dataset.count;
  });
});

difficultyButtons.forEach((btn) => {
  btn.addEventListener("click", () => {
    difficultyButtons.forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");
    selectedDifficulty = btn.dataset.difficulty;
  });
});

scopeButtons.forEach((btn) => {
  btn.addEventListener("click", () => {
    scopeButtons.forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");
    selectedScope = btn.dataset.scope;
  });
});

function renderSuggestionStatus(text) {
  suggestionsEl.innerHTML = "";
  activeSuggestionIndex = -1;
  const li = document.createElement("li");
  li.textContent = text;
  li.className = "suggestion-status";
  suggestionsEl.appendChild(li);
  suggestionsEl.classList.remove("hidden");
}

function hideSuggestions() {
  suggestionsEl.innerHTML = "";
  activeSuggestionIndex = -1;
  suggestionsEl.classList.add("hidden");
}

function renderSuggestions(items) {
  suggestionsEl.innerHTML = "";
  activeSuggestionIndex = -1;

  if (!items || items.length === 0) {
    renderSuggestionStatus("見つかりませんでした");
    return;
  }

  items.forEach((text) => {
    const li = document.createElement("li");
    li.textContent = text;
    li.addEventListener("mousedown", (e) => {
      e.preventDefault();
      artistInput.value = text;
      artistClearBtn.classList.remove("hidden");
      suggestionsEl.classList.add("hidden");
      setArtistConfirmed(true);
    });
    suggestionsEl.appendChild(li);
  });

  suggestionsEl.classList.remove("hidden");
}

function highlightSuggestion(delta) {
  const items = Array.from(suggestionsEl.children);
  // Nothing selectable while showing a "検索中…" / "見つかりませんでした" placeholder.
  if (items.length === 0 || items[0].classList.contains("suggestion-status")) return;

  items[activeSuggestionIndex]?.classList.remove("active");
  activeSuggestionIndex =
    (activeSuggestionIndex + delta + items.length) % items.length;
  const active = items[activeSuggestionIndex];
  active.classList.add("active");
  artistInput.value = active.textContent;
  artistClearBtn.classList.remove("hidden");
}

async function fetchSuggestions(query) {
  // Cancel any suggestion request still in flight so a slow response for an older,
  // shorter query can never arrive after (and clobber the dropdown with stale
  // results for) a newer, more specific one.
  if (suggestAbortController) {
    suggestAbortController.abort();
  }
  const controller = new AbortController();
  suggestAbortController = controller;

  // Belt-and-suspenders alongside the abort above: only ever render the response
  // for the most recently issued request, in case an older one's promise still
  // settles (e.g. the abort raced the response).
  const requestId = ++suggestRequestId;

  try {
    const res = await fetch(`/api/suggest?q=${encodeURIComponent(query)}`, {
      signal: controller.signal,
    });
    if (requestId !== suggestRequestId) return;
    if (!res.ok) return renderSuggestions([]);
    const data = await res.json();
    if (requestId !== suggestRequestId) return;
    renderSuggestions(data);
  } catch (err) {
    if (requestId !== suggestRequestId) return;
    if (err.name !== "AbortError") {
      renderSuggestions([]);
    }
  }
}

artistInput.addEventListener("input", () => {
  setArtistConfirmed(false);
  const query = artistInput.value.trim();
  artistClearBtn.classList.toggle("hidden", query.length === 0);
  clearTimeout(suggestDebounceTimer);

  if (query.length < 1) {
    if (suggestAbortController) {
      suggestAbortController.abort();
    }
    hideSuggestions();
    return;
  }

  // Show a status right away so it's clear something is happening during the
  // debounce + network round-trip, instead of the dropdown just staying empty
  // (which looked identical whether it was still searching or found nothing).
  renderSuggestionStatus("検索中…");

  // A short debounce -- imperceptible to someone typing, but it keeps fast typing
  // from firing a full suggestion lookup (which resolves each candidate against
  // YouTube Music, multiple requests per keystroke) on every single character,
  // which was piling up enough in-flight server work to delay the one response
  // that actually matters.
  suggestDebounceTimer = setTimeout(() => fetchSuggestions(query), 100);
});

artistClearBtn.addEventListener("mousedown", (e) => {
  // mousedown (not click) so this fires before the input's blur hides everything
  e.preventDefault();
  artistInput.value = "";
  artistClearBtn.classList.add("hidden");
  setArtistConfirmed(false);
  hideSuggestions();
  artistInput.focus();
});

artistInput.addEventListener("blur", () => {
  setTimeout(() => suggestionsEl.classList.add("hidden"), 100);
});

artistInput.addEventListener("focus", () => {
  if (!artistConfirmed && suggestionsEl.children.length > 0) {
    suggestionsEl.classList.remove("hidden");
  }
});

function updateProgress() {
  progressEl.textContent = `${questionIndex + 1} / ${questions.length}問`;
  const pct = ((questionIndex + 1) / questions.length) * 100;
  progressBarFillEl.style.width = `${pct}%`;
}

let selectedChoiceIndex = -1;

function updateChoiceHighlight() {
  Array.from(choicesEl.children).forEach((btn, i) => {
    btn.classList.toggle("keyboard-selected", i === selectedChoiceIndex);
  });
}

function moveChoiceSelection(delta) {
  const items = Array.from(choicesEl.children);
  if (items.length === 0 || items[0].disabled) return;
  selectedChoiceIndex = (selectedChoiceIndex + delta + items.length) % items.length;
  updateChoiceHighlight();
}

// The snippet box's height is fixed (CSS gives it whatever space is left
// over after the topbar/choices/feedback/next-button, so it never resizes
// per question -- see style.css). But exactly how much that leftover space
// is can't be predicted in CSS alone, so instead of ever letting it scroll,
// shrink the text (not the box) until it actually fits.
const SNIPPET_MIN_FONT_PX = 12;

function fitSnippetText() {
  snippetEl.style.fontSize = "";
  const maxHeight = snippetEl.clientHeight;
  if (!maxHeight) return;
  let fontSize = parseFloat(getComputedStyle(snippetEl).fontSize);
  while (snippetEl.scrollHeight > maxHeight + 1 && fontSize > SNIPPET_MIN_FONT_PX) {
    fontSize -= 1;
    snippetEl.style.fontSize = `${fontSize}px`;
  }
}

window.addEventListener("resize", () => {
  if (!quizSection.classList.contains("hidden")) {
    fitSnippetText();
  }
});

function showQuestion() {
  const q = questions[questionIndex];
  feedbackEl.textContent = "";
  nextBtn.classList.add("invisible");
  nextBtn.textContent =
    questionIndex === questions.length - 1 ? "結果を見る" : "次の問題";
  choicesEl.innerHTML = "";
  selectedChoiceIndex = -1;

  snippetEl.textContent = q.snippet;
  fitSnippetText();
  q.choices.forEach((choice) => {
    const btn = document.createElement("button");
    btn.textContent = choice;
    btn.addEventListener("click", () => submitAnswer(q, choice, btn));
    choicesEl.appendChild(btn);
  });

  updateProgress();
}

function showResults() {
  document.body.classList.remove("quiz-active");
  quizSection.classList.add("hidden");
  scoreSection.classList.add("hidden");
  resultsScreen.classList.remove("hidden");

  resultsScoreEl.textContent = `${score.correct} / ${score.total} 問正解`;

  resultsReviewEl.innerHTML = "";

  const mistakeCount = reviewLog.filter((entry) => !entry.correct).length;
  if (mistakeCount === 0) {
    const perfect = document.createElement("p");
    perfect.className = "results-perfect";
    perfect.textContent = "全問正解！🎉";
    resultsReviewEl.appendChild(perfect);
  }

  const heading = document.createElement("p");
  heading.className = "results-review-heading";
  heading.textContent = `回答一覧 (${reviewLog.length}問)`;
  resultsReviewEl.appendChild(heading);

  reviewLog.forEach((entry) => {
    const item = document.createElement("div");
    item.className = `review-item ${entry.correct ? "review-item-correct" : "review-item-incorrect"}`;

    const snippet = document.createElement("p");
    snippet.className = "review-snippet";
    snippet.textContent = entry.snippet;
    item.appendChild(snippet);

    const yourAnswer = document.createElement("p");
    yourAnswer.className = entry.correct ? "review-correct-answer" : "review-your-answer";
    yourAnswer.textContent = `あなたの回答: ${entry.userAnswer}`;
    item.appendChild(yourAnswer);

    if (!entry.correct) {
      const correctAnswer = document.createElement("p");
      correctAnswer.className = "review-correct-answer";
      correctAnswer.textContent = `正解: ${entry.correctAnswer}`;
      item.appendChild(correctAnswer);
    }

    resultsReviewEl.appendChild(item);
  });
}

function decodeAnswer(b64) {
  const bytes = Uint8Array.from(atob(b64), (c) => c.charCodeAt(0));
  return new TextDecoder("utf-8").decode(bytes);
}

function submitAnswer(question, choice, clickedBtn) {
  Array.from(choicesEl.children).forEach((btn) => (btn.disabled = true));

  const answer = decodeAnswer(question.a);
  const correct = choice === answer;

  score.total += 1;
  if (correct) {
    score.correct += 1;
    clickedBtn.classList.add("correct");
    feedbackEl.textContent = "正解！";
  } else {
    clickedBtn.classList.add("incorrect");
    feedbackEl.textContent = `不正解… 正解は「${answer}」`;
    Array.from(choicesEl.children).forEach((btn) => {
      if (btn.textContent === answer) btn.classList.add("correct");
    });
  }

  reviewLog.push({
    snippet: question.snippet,
    userAnswer: choice,
    correctAnswer: answer,
    correct,
  });

  scoreCorrectEl.textContent = score.correct;
  scoreTotalEl.textContent = score.total;
  nextBtn.classList.remove("invisible");

  feedbackEl.classList.remove("pop");
  void feedbackEl.offsetWidth; // restart the pop animation even if it just played
  feedbackEl.classList.add("pop");
}

nextBtn.addEventListener("click", () => {
  questionIndex += 1;
  if (questionIndex >= questions.length) {
    showResults();
  } else {
    showQuestion();
  }
});

let currentLoadController = null;

function sleep(ms, signal) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(resolve, ms);
    if (signal) {
      signal.addEventListener("abort", () => {
        clearTimeout(timer);
        reject(new DOMException("aborted", "AbortError"));
      });
    }
  });
}

const POLL_INTERVAL_MS = 600;

async function pollJobUntilDone(jobId, signal) {
  while (true) {
    const res = await fetch(`/api/quiz/progress/${jobId}`, { signal });
    if (!res.ok) {
      return { status: "error" };
    }
    const data = await res.json();
    if (data.status === "running") {
      if (data.total > 0) {
        loadingTextEl.textContent = `問題を読み込み中... (${data.current}/${data.total}曲確認)`;
        loadingProgressBarEl.classList.remove("hidden");
        loadingProgressFillEl.style.width = `${(data.current / data.total) * 100}%`;
      } else {
        loadingTextEl.textContent = "曲を検索中...";
      }
      await sleep(POLL_INTERVAL_MS, signal);
      continue;
    }
    return data;
  }
}

async function startQuiz(artist, count, difficulty, scope) {
  suggestionsEl.classList.add("hidden");
  document.body.classList.remove("quiz-active");
  quizSection.classList.add("hidden");
  scoreSection.classList.add("hidden");
  resultsScreen.classList.add("hidden");
  statusEl.textContent = "";
  loadingTextEl.textContent = "曲を検索中...";
  loadingProgressBarEl.classList.add("hidden");
  loadingProgressFillEl.style.width = "0%";
  loadingEl.classList.remove("hidden");
  startBtn.disabled = true;
  setSetupControlsDisabled(true);

  const controller = new AbortController();
  currentLoadController = controller;

  try {
    const buildRes = await fetch("/api/quiz/build", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ artist, count, difficulty, scope }),
      signal: controller.signal,
    });

    if (!buildRes.ok) {
      setupScreen.classList.remove("hidden");
      statusEl.textContent =
        "問題を取得できませんでした。アーティスト名を確認するか、もう一度試してください。";
      return;
    }

    const { job_id: jobId } = await buildRes.json();
    const data = await pollJobUntilDone(jobId, controller.signal);

    if (data.status !== "done") {
      setupScreen.classList.remove("hidden");
      statusEl.textContent =
        "問題を取得できませんでした。アーティスト名を確認するか、もう一度試してください。";
      return;
    }

    questions = data.questions;
    questionIndex = 0;
    score = { correct: 0, total: 0 };
    reviewLog = [];
    lastArtist = artist;
    lastCount = count;
    lastDifficulty = difficulty;
    lastScope = scope;
    scoreCorrectEl.textContent = "0";
    scoreTotalEl.textContent = "0";
    scoreArtistEl.textContent = data.artist;

    setupScreen.classList.add("hidden");
    scoreSection.classList.remove("hidden");
    quizSection.classList.remove("hidden");
    document.body.classList.add("quiz-active");
    showQuestion();
  } catch (err) {
    setupScreen.classList.remove("hidden");
    if (err.name !== "AbortError") {
      statusEl.textContent = "通信エラーが発生しました。もう一度試してください。";
    }
  } finally {
    currentLoadController = null;
    loadingEl.classList.add("hidden");
    setSetupControlsDisabled(false);
    startBtn.disabled = !artistConfirmed;
  }
}

startBtn.addEventListener("click", () => {
  const artist = artistInput.value.trim();
  if (!artist) return;
  startQuiz(artist, selectedCount, selectedDifficulty, selectedScope);
});

cancelLoadBtn.addEventListener("click", () => {
  if (currentLoadController) {
    currentLoadController.abort();
  }
});

artistInput.addEventListener("keydown", (e) => {
  const suggestionsVisible = !suggestionsEl.classList.contains("hidden");

  if (e.key === "ArrowDown" && suggestionsVisible) {
    e.preventDefault();
    highlightSuggestion(1);
  } else if (e.key === "ArrowUp" && suggestionsVisible) {
    e.preventDefault();
    highlightSuggestion(-1);
  } else if (e.key === "Escape") {
    suggestionsEl.classList.add("hidden");
  } else if (e.key === "Enter") {
    e.preventDefault();
    if (suggestionsVisible && activeSuggestionIndex >= 0) {
      // A suggestion has been explicitly picked with the arrow keys -- confirm it
      // and start right away.
      const items = Array.from(suggestionsEl.children);
      artistInput.value = items[activeSuggestionIndex].textContent;
      artistClearBtn.classList.remove("hidden");
      suggestionsEl.classList.add("hidden");
      setArtistConfirmed(true);
      startBtn.click();
    } else if (artistConfirmed) {
      // Already confirmed via a click or arrow-key selection earlier -- start.
      startBtn.click();
    }
    // Otherwise Enter does nothing: typing "official" and hitting Enter should not
    // start a quiz for the literal text -- an artist must be chosen from the
    // suggestion list first (by clicking it or arrowing to it).
  }
});

playAgainBtn.addEventListener("click", () => {
  startQuiz(lastArtist, lastCount, lastDifficulty, lastScope);
});

homeBtn.addEventListener("click", () => {
  resultsScreen.classList.add("hidden");
  setupScreen.classList.remove("hidden");
});

quizHomeBtn.addEventListener("click", () => {
  document.body.classList.remove("quiz-active");
  quizSection.classList.add("hidden");
  scoreSection.classList.add("hidden");
  setupScreen.classList.remove("hidden");
});

document.addEventListener("keydown", (e) => {
  if (quizSection.classList.contains("hidden")) return;

  if (e.key === "ArrowDown") {
    e.preventDefault();
    moveChoiceSelection(1);
    return;
  }
  if (e.key === "ArrowUp") {
    e.preventDefault();
    moveChoiceSelection(-1);
    return;
  }
  if (e.key !== "Enter") return;

  if (!nextBtn.classList.contains("invisible")) {
    e.preventDefault();
    nextBtn.click();
    return;
  }

  const items = Array.from(choicesEl.children);
  const chosen = items[selectedChoiceIndex];
  if (chosen && !chosen.disabled) {
    e.preventDefault();
    chosen.click();
  }
});
