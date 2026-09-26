const state = {
  currentGameId: null,
  currentGame: null,
  currentTournamentId: null,
  focusedTournamentGameId: null,
  latestTournament: null,
  tournamentSettings: null,
  // Poll rate control: the server value we last saw, and the value the operator
  // is editing. Background refreshes must never overwrite an in-progress edit.
  serverPollRateMs: null,
  draftPollRateMs: null,
  pollRateDirty: false,
  gamePollHandle: null,
  tournamentPollHandle: null,
};

const boardElement = document.getElementById("board");
const boardEmptyStateElement = document.getElementById("board-empty-state");
const gamesListElement = document.getElementById("games-list");
const botListElement = document.getElementById("bot-list");
const botCountElement = document.getElementById("bot-count");
const tournamentSummaryElement = document.getElementById("tournament-summary");
const playoffBracketPanelElement = document.getElementById("playoff-bracket-panel");
const playoffWinnerElement = document.getElementById("playoff-winner");
const playoffBracketElement = document.getElementById("playoff-bracket");
const tournamentBoardsElement = document.getElementById("tournament-boards");
const tournamentStandingsElement = document.getElementById("tournament-standings");
const tournamentMatchesElement = document.getElementById("tournament-matches");
const messageElement = document.getElementById("message");
const focusCaptionElement = document.getElementById("focus-caption");
const clearFocusButton = document.getElementById("clear-focus");
const spawnBotsFormElement = document.getElementById("spawn-bots-form");
const tournamentFormatElement = document.getElementById("tournament-format");
const pauseBetweenRoundsElement = document.getElementById("pause-between-rounds");
const pauseBetweenRoundsControlElement = document.getElementById("pause-between-rounds-control");
const resumeTournamentButton = document.getElementById("resume-tournament");
const pollRateInputElement = document.getElementById("poll-rate-input");
const pollRateHintElement = document.getElementById("poll-rate-hint");
const pollRateActiveElement = document.getElementById("poll-rate-active");
const pollRatePendingElement = document.getElementById("poll-rate-pending");
const savePollRateButton = document.getElementById("save-poll-rate");
const resetPollRateButton = document.getElementById("reset-poll-rate");

const POLL_RATE_MIN_MS = 25;
const POLL_RATE_MAX_MS = 2000;
const POLL_RATE_DEFAULT_MS = 250;

const TOURNAMENT_FORMAT_LABELS = {
  double_round_robin: "Double round robin",
  single_round_robin: "Single round robin",
  single_elimination: "Single elimination playoff",
};

function showMessage(text, tone = "info") {
  messageElement.textContent = text;
  messageElement.dataset.tone = tone;
}

function prettyPlayer(player) {
  return player === "B" ? "Black" : player === "W" ? "White" : player === "draw" ? "Draw" : "-";
}

function prettyTournamentFormat(format) {
  return TOURNAMENT_FORMAT_LABELS[format] || format || "-";
}

function isTournamentGame(game) {
  return Boolean(game && game.metadata && game.metadata.mode === "tournament");
}

function setMainBoardVisibility(hasSelectedGame) {
  boardElement.classList.toggle("hidden", !hasSelectedGame);
  boardElement.setAttribute("aria-hidden", String(!hasSelectedGame));
  boardEmptyStateElement.classList.toggle("hidden", hasSelectedGame);
}

function resetMainViewport() {
  state.currentGameId = null;
  state.currentGame = null;
  document.getElementById("game-title").textContent = "No game selected";
  document.getElementById("game-subtitle").textContent = "Create a game or focus a tournament board.";
  document.getElementById("score-black").textContent = "-";
  document.getElementById("score-white").textContent = "-";
  document.getElementById("current-player").textContent = "-";
  document.getElementById("game-status").textContent = "-";
  document.getElementById("game-winner").textContent = "-";
  boardElement.innerHTML = "";
  document.getElementById("history").innerHTML = "";
  setMainBoardVisibility(false);
}

function buildPlayerSpec(side) {
  const type = document.getElementById(`${side}-type`).value;
  const spec = { type };
  if (type === "remote") {
    const url = document.getElementById(`${side}-url`).value.trim();
    if (!url) {
      throw new Error(`${side} remote bot needs a URL.`);
    }
    spec.url = url;
  }
  return spec;
}

async function fetchJson(url, options = {}) {
  const response = await fetch(url, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });

  const text = await response.text();
  const data = text ? JSON.parse(text) : {};
  if (!response.ok) {
    throw new Error(data.error || `Request failed with ${response.status}`);
  }
  return data;
}

function formatPollRateMs(valueMs) {
  const milliseconds = Number.parseFloat(valueMs) || 0;
  return `${Math.round(milliseconds)} ms`;
}

function clampPollRateMs(valueMs) {
  const milliseconds = Math.round(Number.parseFloat(valueMs));
  if (!Number.isFinite(milliseconds)) {
    return null;
  }
  return Math.min(POLL_RATE_MAX_MS, Math.max(POLL_RATE_MIN_MS, milliseconds));
}

function isPollRateBeingEdited() {
  return state.pollRateDirty || document.activeElement === pollRateInputElement;
}

// Writes the draft value into the input. Only ever called for local edits or for
// an explicit resync, never from the background refresh while editing.
function writePollRateControls(valueMs) {
  const normalized = String(valueMs);
  if (pollRateInputElement.value !== normalized) {
    pollRateInputElement.value = normalized;
  }
}

function renderPollRateStatus() {
  const serverValue = state.serverPollRateMs;
  pollRateActiveElement.textContent = serverValue === null ? "unknown" : formatPollRateMs(serverValue);

  const draft = state.draftPollRateMs;
  const hasPendingChange = state.pollRateDirty && draft !== null && draft !== serverValue;
  pollRatePendingElement.classList.toggle("hidden", !hasPendingChange);
  if (hasPendingChange) {
    pollRatePendingElement.textContent = `Not applied yet: ${formatPollRateMs(draft)}`;
  }

  savePollRateButton.disabled = !hasPendingChange;
  resetPollRateButton.disabled = !hasPendingChange;
}

function renderTournamentSettings(settings) {
  state.tournamentSettings = settings || null;
  const serverValue = settings ? clampPollRateMs(settings.external_bot_request_interval_ms) : null;
  const previousServerValue = state.serverPollRateMs;
  state.serverPollRateMs = serverValue;

  if (!settings) {
    pollRateHintElement.innerHTML = `Default: ${POLL_RATE_DEFAULT_MS} ms. External bots should follow <code>Retry-After</code> if this rate changes and they receive <code>429 Too Many Requests</code>.`;
  } else {
    pollRateHintElement.innerHTML = "Changes apply immediately, even during a running tournament. Bots should track <code>limit_seconds</code> and always honor <code>Retry-After</code> to avoid overloading the server.";
  }

  // Leave the inputs alone while the operator is dragging or typing, otherwise
  // the periodic refresh would reset the value under their fingers.
  if (!isPollRateBeingEdited()) {
    state.draftPollRateMs = serverValue;
    if (serverValue !== null) {
      writePollRateControls(serverValue);
    }
  } else if (serverValue !== previousServerValue && previousServerValue !== null) {
    showMessage(`Poll rate was changed elsewhere to ${formatPollRateMs(serverValue)}. Your unapplied edit was kept.`, "info");
  }

  renderPollRateStatus();
}

async function focusGame(gameId, { tournamentFocus = false } = {}) {
  try {
    const game = await fetchJson(`/api/games/${gameId}`);
    state.currentGameId = game.game_id;
    state.currentGame = game;
    state.focusedTournamentGameId = tournamentFocus || isTournamentGame(game) ? game.game_id : null;
    renderGame(game);
    renderTournament(state.latestTournament);
    startGamePolling();
  } catch (error) {
    if (state.focusedTournamentGameId === gameId) {
      state.focusedTournamentGameId = null;
    }
    stopGamePolling();
    resetMainViewport();
    renderTournament(state.latestTournament);
    showMessage(error.message, "error");
  }
}

async function createGame(event) {
  event.preventDefault();
  try {
    const payload = {
      black: buildPlayerSpec("black"),
      white: buildPlayerSpec("white"),
    };
    const game = await fetchJson("/api/games", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    state.focusedTournamentGameId = null;
    state.currentGameId = game.game_id;
    state.currentGame = game;
    renderGame(game);
    renderTournament(state.latestTournament);
    await refreshGamesList();
    startGamePolling();
    showMessage(`Game ${game.game_id} created.`, "success");
  } catch (error) {
    showMessage(error.message, "error");
  }
}

function renderGame(game) {
  state.currentGameId = game.game_id;
  state.currentGame = game;
  setMainBoardVisibility(true);
  const titleSuffix = isTournamentGame(game) ? ` · tournament match #${game.metadata.match_index}` : "";
  document.getElementById("game-title").textContent = `Game ${game.game_id}${titleSuffix}`;

  if (isTournamentGame(game)) {
    document.getElementById("game-subtitle").textContent = `${game.metadata.black_bot_name} (black) vs ${game.metadata.white_bot_name} (white)`;
  } else {
    document.getElementById("game-subtitle").textContent = `${game.players.black.type} (black) vs ${game.players.white.type} (white)`;
  }

  document.getElementById("score-black").textContent = game.scores.black;
  document.getElementById("score-white").textContent = game.scores.white;
  document.getElementById("current-player").textContent = prettyPlayer(game.current_player);
  document.getElementById("game-status").textContent = game.status;
  document.getElementById("game-winner").textContent = game.winner ? prettyPlayer(game.winner) : "-";

  const legalMoves = new Set(game.legal_moves.map((move) => `${move.row}:${move.col}`));
  const lastMoveKey = game.last_move ? `${game.last_move.row}:${game.last_move.col}` : null;
  const currentSide = game.current_player === "B" ? "black" : "white";
  const currentPlayerConfig = game.players && game.players[currentSide];
  const isHumanTurn = game.status === "active" && currentPlayerConfig && currentPlayerConfig.type === "human";

  boardElement.innerHTML = "";
  game.board.forEach((row, rowIndex) => {
    row.forEach((cell, colIndex) => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "square";
      button.dataset.row = rowIndex;
      button.dataset.col = colIndex;
      button.setAttribute("role", "gridcell");
      button.title = `Row ${rowIndex + 1}, column ${colIndex + 1}`;

      const key = `${rowIndex}:${colIndex}`;
      const isLegal = legalMoves.has(key) && game.status === "active";
      if (isLegal) {
        button.classList.add("legal");
      }
      if (key === lastMoveKey) {
        button.classList.add("last-move");
        button.title = `${button.title} · latest move`;
      }

      if (cell !== ".") {
        const piece = document.createElement("span");
        piece.className = `piece ${cell === "B" ? "black" : "white"}`;
        button.appendChild(piece);
      }

      if (isHumanTurn && isLegal) {
        button.addEventListener("click", () => playMove(rowIndex, colIndex));
      } else {
        button.disabled = true;
      }

      boardElement.appendChild(button);
    });
  });

  const historyElement = document.getElementById("history");
  historyElement.innerHTML = "";
  [...game.history].slice(-12).reverse().forEach((entry) => {
    const item = document.createElement("li");
    if (entry.forfeit) {
      item.textContent = `${prettyPlayer(entry.player)} forfeited (${entry.reason}).`;
    } else if (entry.pass) {
      item.textContent = `${prettyPlayer(entry.player)} passed.`;
    } else {
      item.textContent = `${prettyPlayer(entry.player)} → (${entry.move.row + 1}, ${entry.move.col + 1})`;
    }
    historyElement.appendChild(item);
  });
}

async function refreshGamesList() {
  try {
    const payload = await fetchJson("/api/games");
    gamesListElement.innerHTML = "";
    payload.games.forEach((game) => {
      const item = document.createElement("li");
      const button = document.createElement("button");
      button.type = "button";
      button.className = "game-link";
      const isTournamentMatch = Boolean(game.metadata && game.metadata.mode === "tournament");
      const matchLabel = isTournamentMatch ? `tournament #${game.metadata.match_index}` : "game";
      button.textContent = `${game.game_id} · ${game.players.black.type} vs ${game.players.white.type} · ${game.status} · ${matchLabel}`;
      button.addEventListener("click", async () => {
        await focusGame(game.game_id, { tournamentFocus: isTournamentMatch });
      });
      item.appendChild(button);
      gamesListElement.appendChild(item);
    });
  } catch (error) {
    showMessage(error.message, "error");
  }
}

async function playMove(row, col) {
  if (!state.currentGameId || !state.currentGame || state.currentGame.status !== "active") {
    return;
  }
  try {
    const game = await fetchJson(`/api/games/${state.currentGameId}/move`, {
      method: "POST",
      body: JSON.stringify({ row, col }),
    });
    renderGame(game);
    await refreshGamesList();
    await refreshTournamentView();
    if (game.status === "finished") {
      showMessage(`Game ${game.game_id} finished.`, "success");
    }
  } catch (error) {
    showMessage(error.message, "error");
  }
}

async function removeBot(botId, botName) {
  try {
    await fetchJson(`/api/tournament/bots/${botId}`, { method: "DELETE" });
    await refreshTournamentView();
    showMessage(`Removed bot ${botName}.`, "success");
  } catch (error) {
    showMessage(error.message, "error");
  }
}

async function spawnBots(event) {
  event.preventDefault();
  try {
    const namePrefix = document.getElementById("spawn-name-prefix").value.trim();
    const count = Number.parseInt(document.getElementById("spawn-count").value, 10) || 1;
    const pollIntervalSeconds = (state.serverPollRateMs || POLL_RATE_DEFAULT_MS) / 1000;
    const result = await fetchJson("/api/tournament/spawn-bots", {
      method: "POST",
      body: JSON.stringify({ name_prefix: namePrefix, count, poll_interval: pollIntervalSeconds }),
    });
    await refreshTournamentView();
    const names = result.spawned.map((bot) => bot.name).join(", ");
    showMessage(`Spawned ${result.spawned.length} local bot(s): ${names}.`, "success");
  } catch (error) {
    showMessage(error.message, "error");
  }
}

function handlePollRateInput(event) {
  const rawValue = event.target.value;
  // Allow a temporarily empty or half-typed number box without fighting the user.
  if (rawValue.trim() === "") {
    state.pollRateDirty = true;
    state.draftPollRateMs = null;
    savePollRateButton.disabled = true;
    resetPollRateButton.disabled = false;
    return;
  }

  const value = clampPollRateMs(rawValue);
  if (value === null) {
    return;
  }

  // Never write back into the box while it is being typed into.
  state.pollRateDirty = true;
  state.draftPollRateMs = value;
  renderPollRateStatus();
}

function handlePollRateBlur() {
  // Snap an out-of-range or empty entry back to something valid on blur.
  if (state.draftPollRateMs === null) {
    resetPollRate();
    return;
  }
  writePollRateControls(state.draftPollRateMs);
  renderPollRateStatus();
}

function handlePollRateKeydown(event) {
  if (event.key === "Enter") {
    event.preventDefault();
    savePollRate();
  } else if (event.key === "Escape") {
    event.preventDefault();
    resetPollRate();
  }
}

function resetPollRate() {
  state.pollRateDirty = false;
  state.draftPollRateMs = state.serverPollRateMs;
  if (state.serverPollRateMs !== null) {
    writePollRateControls(state.serverPollRateMs);
  }
  renderPollRateStatus();
}

async function savePollRate() {
  const valueMs = state.draftPollRateMs;
  if (valueMs === null) {
    showMessage(`Poll rate must be between ${POLL_RATE_MIN_MS} and ${POLL_RATE_MAX_MS} ms.`, "error");
    return;
  }

  savePollRateButton.disabled = true;
  try {
    const settings = await fetchJson("/api/tournament/settings", {
      method: "PUT",
      body: JSON.stringify({ external_bot_request_interval_ms: valueMs }),
    });
    // Clear the dirty flag first so the render adopts the newly saved value.
    state.pollRateDirty = false;
    renderTournamentSettings(settings);
    showMessage(`Tournament bot poll rate updated to ${formatPollRateMs(settings.external_bot_request_interval_ms)}.`, "success");
  } catch (error) {
    renderPollRateStatus();
    showMessage(error.message, "error");
  }
}

function formatIdleSeconds(seconds) {
  if (typeof seconds !== "number" || !isFinite(seconds)) {
    return "unknown";
  }
  if (seconds < 1) {
    return "just now";
  }
  if (seconds < 60) {
    return `${Math.floor(seconds)}s ago`;
  }
  if (seconds < 3600) {
    return `${Math.floor(seconds / 60)}m ago`;
  }
  return `${Math.floor(seconds / 3600)}h ago`;
}

function renderBots(bots) {
  botListElement.innerHTML = "";
  const staleCount = bots.filter((bot) => bot.stale).length;
  const liveCount = bots.length - staleCount;
  botCountElement.textContent = staleCount
    ? `${liveCount} of ${bots.length} responding · ${staleCount} silent`
    : `${bots.length} connected`;
  botCountElement.classList.toggle("has-stale", staleCount > 0);

  if (!bots.length) {
    const empty = document.createElement("li");
    empty.textContent = "No bots connected yet.";
    botListElement.appendChild(empty);
    return;
  }

  bots.forEach((bot) => {
    const item = document.createElement("li");
    item.className = `bot-row${bot.stale ? " stale-bot" : ""}`;

    const details = document.createElement("div");
    details.className = "bot-row-details";
    const managedLabel = bot.managed ? ` · local pid ${bot.pid}` : "";
    const lastSeenClock = bot.last_seen_at ? new Date(bot.last_seen_at).toLocaleTimeString() : "never";
    const idleLabel = formatIdleSeconds(bot.seconds_since_last_seen);
    const staleBadge = bot.stale
      ? `<span class="stale-badge">Silent for ${idleLabel.replace(" ago", "")}</span>`
      : "";
    details.innerHTML = `
      <strong>${bot.name}</strong>${staleBadge}
      <span class="muted">${bot.bot_id}</span>
      <span class="muted">last seen ${lastSeenClock} · ${idleLabel}${managedLabel}</span>
    `;

    const removeButton = document.createElement("button");
    removeButton.type = "button";
    removeButton.className = "danger-button";
    removeButton.textContent = bot.managed ? "Stop & remove" : "Remove";
    removeButton.addEventListener("click", () => removeBot(bot.bot_id, bot.name));

    item.appendChild(details);
    item.appendChild(removeButton);
    botListElement.appendChild(item);
  });
}

function createMiniBoard(match) {
  const card = document.createElement("article");
  card.className = `mini-board-card${state.focusedTournamentGameId === match.game_id ? " focused-mini-board" : ""}`;
  card.tabIndex = 0;
  card.setAttribute("role", "button");
  card.addEventListener("click", () => focusGame(match.game_id, { tournamentFocus: true }));
  card.addEventListener("keydown", (event) => {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      focusGame(match.game_id, { tournamentFocus: true });
    }
  });

  const header = document.createElement("div");
  header.className = "mini-board-header";
  header.innerHTML = `
    <strong>${match.black.name} vs ${match.white.name}</strong>
    <span class="muted">${match.game_id} · turn ${prettyPlayer(match.current_player)}</span>
  `;
  card.appendChild(header);

  const board = document.createElement("div");
  board.className = "mini-board";
  const legalMoves = new Set((match.legal_moves || []).map((move) => `${move.row}:${move.col}`));
  const lastMoveKey = match.last_move ? `${match.last_move.row}:${match.last_move.col}` : null;
  match.board.forEach((row, rowIndex) => {
    row.forEach((cell, colIndex) => {
      const square = document.createElement("div");
      square.className = "mini-square";
      if (legalMoves.has(`${rowIndex}:${colIndex}`)) {
        square.classList.add("legal");
      }
      if (`${rowIndex}:${colIndex}` === lastMoveKey) {
        square.classList.add("last-move");
      }
      if (cell !== ".") {
        const piece = document.createElement("span");
        piece.className = `piece ${cell === "B" ? "black" : "white"}`;
        square.appendChild(piece);
      }
      board.appendChild(square);
    });
  });
  card.appendChild(board);

  const footer = document.createElement("div");
  footer.className = "mini-board-footer muted";
  const roundLabel = match.round_name ? `${match.round_name} · ` : "";
  footer.textContent = `${roundLabel}Match #${match.match_index} · ${match.status} · score ${match.scores.black}:${match.scores.white}`;
  card.appendChild(footer);

  return card;
}

function renderOngoingBoards(tournament) {
  tournamentBoardsElement.innerHTML = "";
  const ongoingMatches = tournament && tournament.ongoing_matches ? tournament.ongoing_matches : [];
  if (!ongoingMatches.length) {
    tournamentBoardsElement.textContent = "No tournament games are running.";
    return;
  }

  ongoingMatches.forEach((match) => {
    tournamentBoardsElement.appendChild(createMiniBoard(match));
  });
}

function renderFocusUi(tournament) {
  const matches = tournament && tournament.matches ? tournament.matches : [];
  const focusedMatch = matches.find((match) => match.game_id === state.focusedTournamentGameId);
  clearFocusButton.classList.toggle("hidden", !state.focusedTournamentGameId);

  if (focusedMatch) {
    focusCaptionElement.textContent = `Focused match #${focusedMatch.match_index}: ${focusedMatch.black.name} vs ${focusedMatch.white.name}. Click another board to switch focus.`;
    return;
  }

  const hasOngoingMatches = Boolean(tournament && tournament.ongoing_matches && tournament.ongoing_matches.length);
  focusCaptionElement.textContent = hasOngoingMatches
    ? "Click any running tournament board to focus it above."
    : "Tournament boards will appear here. Click one to focus it above.";
}

function createBracketMatch(match) {
  const isInteractive = Boolean(match.game_id);
  const card = document.createElement(isInteractive ? "button" : "article");
  card.className = `bracket-match bracket-status-${match.status}${match.active ? " active-match" : ""}`;

  if (isInteractive) {
    card.type = "button";
    card.addEventListener("click", () => focusGame(match.game_id, { tournamentFocus: true }));
  }

  const heading = document.createElement("div");
  heading.className = "bracket-match-heading muted";
  heading.textContent = `Match #${match.match_index} · ${match.status}`;
  card.appendChild(heading);

  [
    { color: "Black", participant: match.black },
    { color: "White", participant: match.white },
  ].forEach(({ color, participant }) => {
    const row = document.createElement("div");
    row.className = `bracket-player${participant.bot_id && participant.bot_id === match.winner_bot_id ? " bracket-player-winner" : ""}`;

    const colorChip = document.createElement("span");
    colorChip.className = "bracket-color muted";
    colorChip.textContent = color;

    const name = document.createElement("strong");
    name.textContent = participant.name || participant.label || "TBD";

    row.appendChild(colorChip);
    row.appendChild(name);
    card.appendChild(row);
  });

  const footer = document.createElement("div");
  footer.className = "bracket-match-footer muted";
  if (match.winner_name) {
    const scoreSuffix = match.scores ? ` · ${match.scores.black}:${match.scores.white}` : "";
    footer.textContent = `Advances: ${match.winner_name}${scoreSuffix}`;
  } else if (match.scores) {
    footer.textContent = `Score ${match.scores.black}:${match.scores.white}`;
  } else {
    footer.textContent = isInteractive ? "Click to focus this board." : "Awaiting previous round.";
  }
  card.appendChild(footer);

  return card;
}

function renderPlayoffBracket(tournament) {
  const bracket = tournament && tournament.format === "single_elimination" ? tournament.bracket : null;
  playoffBracketPanelElement.classList.toggle("hidden", !bracket);

  if (!bracket) {
    playoffWinnerElement.textContent = "Winner will appear here when the final is decided.";
    playoffBracketElement.innerHTML = "";
    return;
  }

  playoffWinnerElement.textContent = tournament.champion
    ? `Champion: ${tournament.champion.name} won the playoff.`
    : tournament.status === "finished"
      ? "Tournament finished. Champion unavailable."
      : "Winner will appear here when the final is decided.";

  playoffBracketElement.innerHTML = "";
  bracket.rounds.forEach((round) => {
    const column = document.createElement("section");
    column.className = "bracket-round";

    const title = document.createElement("h5");
    title.textContent = round.round_name;
    column.appendChild(title);

    const list = document.createElement("div");
    list.className = "bracket-round-matches";
    round.matches.forEach((match) => {
      list.appendChild(createBracketMatch(match));
    });
    column.appendChild(list);

    playoffBracketElement.appendChild(column);
  });
}

function renderTournament(tournament) {
  state.currentTournamentId = tournament && tournament.tournament_id ? tournament.tournament_id : null;
  state.latestTournament = tournament || null;

  if (!tournament) {
    tournamentSummaryElement.textContent = "No tournament started yet.";
    resumeTournamentButton.classList.add("hidden");
    playoffBracketPanelElement.classList.add("hidden");
    playoffWinnerElement.textContent = "Winner will appear here when the final is decided.";
    playoffBracketElement.innerHTML = "";
    tournamentBoardsElement.textContent = "No tournament games are running.";
    tournamentStandingsElement.innerHTML = "";
    tournamentMatchesElement.innerHTML = "";
    renderFocusUi(null);
    return;
  }

  const activeLabel = tournament.ongoing_match_count
    ? `${tournament.ongoing_match_count} ongoing matches`
    : "No active match.";
  const championLabel = tournament.champion ? `<br>Champion: ${tournament.champion.name}` : "";
  const pausedLabel = tournament.paused
    ? `<br><strong class="paused-label">Paused before the ${tournament.paused_before_round_name}.</strong>`
    : "";
  tournamentSummaryElement.innerHTML = `
    <strong>${tournament.tournament_id}</strong><br>
    Status: ${tournament.status}<br>
    Format: ${prettyTournamentFormat(tournament.format)}<br>
    Progress: ${tournament.completed_match_count}/${tournament.match_count} matches finished<br>
    ${activeLabel}${championLabel}${pausedLabel}
  `;
  resumeTournamentButton.classList.toggle("hidden", !tournament.paused);
  if (tournament.paused) {
    resumeTournamentButton.textContent = `Resume · start the ${tournament.paused_before_round_name}`;
  }
  renderPlayoffBracket(tournament);
  renderOngoingBoards(tournament);
  renderFocusUi(tournament);

  tournamentStandingsElement.innerHTML = "";
  tournament.standings.forEach((entry) => {
    const item = document.createElement("li");
    item.className = "standing-row";
    item.innerHTML = `
      <span><strong>${entry.name}</strong> <span class="muted">${entry.bot_id}</span></span>
      <span>${entry.points.toFixed(1)} pts · ${entry.wins}-${entry.draws}-${entry.losses} · discs ${entry.pieces_for}/${entry.pieces_against}</span>
    `;
    tournamentStandingsElement.appendChild(item);
  });

  tournamentMatchesElement.innerHTML = "";
  tournament.matches.forEach((match) => {
    const item = document.createElement("li");
    item.className = `match-row${match.status === "active" ? " active-match" : ""}`;
    const button = document.createElement("button");
    button.type = "button";
    button.className = `game-link${state.focusedTournamentGameId === match.game_id ? " focused-link" : ""}`;
    const roundPrefix = match.round_name ? `${match.round_name} · ` : "";
    button.textContent = `${roundPrefix}#${match.match_index} · ${match.black.name} vs ${match.white.name} · ${match.status}`;
    button.addEventListener("click", () => focusGame(match.game_id, { tournamentFocus: true }));
    item.appendChild(button);
    tournamentMatchesElement.appendChild(item);
  });
}

async function refreshTournamentView() {
  try {
    const [botPayload, tournamentPayload, settingsPayload] = await Promise.all([
      fetchJson("/api/tournament/bots"),
      fetchJson("/api/tournaments"),
      fetchJson("/api/tournament/settings"),
    ]);
    renderBots(botPayload.bots || []);
    renderTournament((tournamentPayload.tournaments || [])[0] || null);
    renderTournamentSettings(settingsPayload);
  } catch (error) {
    showMessage(error.message, "error");
  }
}

async function startTournament() {
  try {
    const tournamentFormat = tournamentFormatElement.value;
    const pauseBetweenRounds = tournamentFormat === "single_elimination" && pauseBetweenRoundsElement.checked;
    const tournament = await fetchJson("/api/tournaments", {
      method: "POST",
      body: JSON.stringify({ tournament_format: tournamentFormat, pause_between_rounds: pauseBetweenRounds }),
    });
    renderTournament(tournament);
    await refreshGamesList();
    startTournamentPolling();
    const firstMatch = tournament.ongoing_matches && tournament.ongoing_matches.length ? tournament.ongoing_matches[0] : null;
    const firstGameId = firstMatch ? firstMatch.game_id : null;
    if (firstGameId) {
      await focusGame(firstGameId, { tournamentFocus: true });
    }
    showMessage(
      tournament.paused
        ? `Tournament ${tournament.tournament_id} started as ${prettyTournamentFormat(tournament.format)} and is paused before the ${tournament.paused_before_round_name}.`
        : `Tournament ${tournament.tournament_id} started as ${prettyTournamentFormat(tournament.format)} with ${tournament.ongoing_match_count} parallel games.`,
      "success",
    );
  } catch (error) {
    showMessage(error.message, "error");
  }
}

async function resumeTournament() {
  if (!state.currentTournamentId) {
    return;
  }
  resumeTournamentButton.disabled = true;
  try {
    const tournament = await fetchJson(`/api/tournaments/${state.currentTournamentId}/resume`, { method: "POST" });
    renderTournament(tournament);
    await refreshGamesList();
    startTournamentPolling();
    const firstMatch = tournament.ongoing_matches && tournament.ongoing_matches.length ? tournament.ongoing_matches[0] : null;
    if (firstMatch) {
      await focusGame(firstMatch.game_id, { tournamentFocus: true });
    }
    showMessage(`Resumed tournament ${tournament.tournament_id} with ${tournament.ongoing_match_count} parallel games.`, "success");
  } catch (error) {
    showMessage(error.message, "error");
  } finally {
    resumeTournamentButton.disabled = false;
  }
}

function syncPauseBetweenRoundsVisibility() {
  const isPlayoff = tournamentFormatElement.value === "single_elimination";
  pauseBetweenRoundsControlElement.classList.toggle("hidden", !isPlayoff);
}

async function pollCurrentGame() {
  if (!state.currentGameId) {
    return;
  }
  try {
    const game = await fetchJson(`/api/games/${state.currentGameId}`);
    state.currentGame = game;
    renderGame(game);
    await refreshGamesList();
  } catch (error) {
    showMessage(error.message, "error");
    stopGamePolling();
    if (isTournamentGame(state.currentGame)) {
      state.focusedTournamentGameId = null;
    }
    resetMainViewport();
    renderTournament(state.latestTournament);
  }
}

async function pollCurrentTournament() {
  try {
    await refreshTournamentView();
  } catch (error) {
    showMessage(error.message, "error");
    stopTournamentPolling();
  }
}

function startGamePolling() {
  stopGamePolling();
  state.gamePollHandle = window.setInterval(pollCurrentGame, 2000);
}

function stopGamePolling() {
  if (state.gamePollHandle) {
    window.clearInterval(state.gamePollHandle);
    state.gamePollHandle = null;
  }
}

function startTournamentPolling() {
  stopTournamentPolling();
  state.tournamentPollHandle = window.setInterval(pollCurrentTournament, 2000);
}

function stopTournamentPolling() {
  if (state.tournamentPollHandle) {
    window.clearInterval(state.tournamentPollHandle);
    state.tournamentPollHandle = null;
  }
}

function clearTournamentFocus() {
  state.focusedTournamentGameId = null;
  if (isTournamentGame(state.currentGame)) {
    stopGamePolling();
    resetMainViewport();
  }
  renderTournament(state.latestTournament);
}

function toggleRemoteUrl(side) {
  const typeElement = document.getElementById(`${side}-type`);
  const label = document.querySelector(`.remote-url[data-target="${side}"]`);
  label.classList.toggle("hidden", typeElement.value !== "remote");
}

document.getElementById("new-game-form").addEventListener("submit", createGame);
document.getElementById("refresh-games").addEventListener("click", refreshGamesList);
document.getElementById("refresh-tournament").addEventListener("click", refreshTournamentView);
document.getElementById("start-tournament").addEventListener("click", startTournament);
resumeTournamentButton.addEventListener("click", resumeTournament);
tournamentFormatElement.addEventListener("change", syncPauseBetweenRoundsVisibility);
document.getElementById("black-type").addEventListener("change", () => toggleRemoteUrl("black"));
document.getElementById("white-type").addEventListener("change", () => toggleRemoteUrl("white"));
clearFocusButton.addEventListener("click", clearTournamentFocus);
spawnBotsFormElement.addEventListener("submit", spawnBots);
pollRateInputElement.addEventListener("input", handlePollRateInput);
pollRateInputElement.addEventListener("blur", handlePollRateBlur);
pollRateInputElement.addEventListener("keydown", handlePollRateKeydown);
savePollRateButton.addEventListener("click", savePollRate);
resetPollRateButton.addEventListener("click", resetPollRate);

toggleRemoteUrl("black");
toggleRemoteUrl("white");
syncPauseBetweenRoundsVisibility();
resetMainViewport();
refreshGamesList();
refreshTournamentView();
startTournamentPolling();
showMessage("Ready. Create a game, register bots, or focus a tournament board.");

