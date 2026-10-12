const tokenInput = document.querySelector("#token");
const status = document.querySelector("#status");
const output = document.querySelector("#portfolio");

chrome.storage.local.get(["apiToken"], (values) => {
  if (values.apiToken) tokenInput.value = values.apiToken;
});

document.querySelector("#connect").addEventListener("click", async () => {
  const token = tokenInput.value.trim();
  if (token.length < 32) {
    status.textContent = "Enter a valid local API token (32 characters minimum).";
    return;
  }
  status.textContent = "Connecting to the local paper API…";
  output.textContent = "";
  try {
    const response = await fetch("http://127.0.0.1:8766/api/portfolio", {
      headers: { Authorization: `Bearer ${token}` },
      cache: "no-store",
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || "API request failed");
    await chrome.storage.local.set({ apiToken: token });
    status.textContent = "PAPER portfolio loaded. Values without fresh marks are unavailable.";
    output.textContent = JSON.stringify(payload, null, 2);
  } catch (error) {
    status.textContent = `Unable to load paper data: ${error.message}`;
  }
});
