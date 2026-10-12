const cards = document.querySelector("#cards");
const statusLine = document.querySelector("#status");
const button = document.querySelector("#refresh");

function makeNode(tag, className, text) {
  const element = document.createElement(tag);
  element.className = className;
  element.textContent = text;
  return element;
}

function makeCard(item) {
  const element = makeNode("article", "card", "");
  element.append(makeNode("h3", "", item.symbol));
  if (item.error) {
    element.append(makeNode("p", "error", item.error));
    return element;
  }

  const price = Number(item.price).toLocaleString(undefined, {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
  element.append(
    makeNode("p", "price", price),
    makeNode("p", "muted", "Latest close · data as of " + new Date(item.as_of).toLocaleString()),
    makeNode("p", "signal", item.signal.direction.replaceAll("_", " ")),
    makeNode("p", "muted", item.signal.explanation),
  );
  return element;
}

async function refresh() {
  button.disabled = true;
  cards.replaceChildren();
  statusLine.textContent = "Loading provider data…";
  statusLine.className = "muted";
  try {
    const response = await fetch("/api/watchlist", { cache: "no-store" });
    if (!response.ok) throw new Error("Analysis service unavailable (" + response.status + ").");
    const { symbols } = await response.json();
    const results = await Promise.all(symbols.map(async (symbol) => {
      try {
        const result = await fetch("/api/market/" + encodeURIComponent(symbol), { cache: "no-store" });
        const payload = await result.json();
        if (!result.ok) throw new Error(payload.detail || "Market data unavailable.");
        return payload;
      } catch (error) {
        return { symbol, error: error.message };
      }
    }));
    cards.replaceChildren(...results.map(makeCard));
    statusLine.textContent = "Provider data and calculated signals. Unavailable symbols are marked.";
  } catch (error) {
    statusLine.textContent = error.message;
    statusLine.className = "error";
  } finally {
    button.disabled = false;
  }
}

button.addEventListener("click", refresh);
refresh();
