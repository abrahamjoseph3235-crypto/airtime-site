(() => {
  "use strict";
  const panel = document.getElementById("reward-status");
  if (!panel) return;
  const statusUrl = panel.dataset.statusUrl;
  const label = document.getElementById("reward-status-label");
  const message = document.getElementById("reward-status-message");
  const linkContainer = document.getElementById("reward-link-container");
  const link = document.getElementById("reward-link");
  const note = document.getElementById("reward-review-note");
  const lastChecked = document.getElementById("last-checked");
  const labels = {
    pending: "Pending review",
    approved: "Approved",
    rejected: "Not approved",
    reward_sent: "Approved"
  };
  let inFlight = false;
  async function refreshStatus() {
    if (inFlight) return;
    inFlight = true;
    try {
      const response = await fetch(statusUrl, {
        method: "GET",
        credentials: "same-origin",
        cache: "no-store",
        headers: { "Accept": "application/json" }
      });
      if (!response.ok) throw new Error("Status request failed");
      const data = await response.json();
      label.textContent = labels[data.status] || "Status unavailable";
      linkContainer.hidden = true;
      link.removeAttribute("href");
      note.hidden = true;
      if (data.status === "pending") {
        message.textContent = "Your screenshots have been received and are waiting for manual review.";
      } else if (data.status === "rejected") {
        message.textContent = "Your submission was not approved. Please read the reviewer note below if one is available.";
        if (data.reviewer_note) {
          note.textContent = "Reviewer note: " + data.reviewer_note;
          note.hidden = false;
        }
      } else if (data.reward_ready && data.reward_url) {
        message.textContent = "Your screenshots are approved and your reward link is ready.";
        link.href = data.reward_url;
        linkContainer.hidden = false;
      } else if (data.status === "approved" || data.status === "reward_sent") {
        message.textContent = "Your screenshots are approved. Your provider-generated claim link has not been added yet. Check back shortly.";
      } else {
        message.textContent = "We could not determine your status. Please try again shortly.";
      }
      lastChecked.textContent = "Last checked: " + new Date().toLocaleTimeString();
    } catch (_) {
      message.textContent = "We couldn't refresh your status just now. We'll try again automatically.";
    } finally {
      inFlight = false;
    }
  }
  refreshStatus();
  window.setInterval(refreshStatus, 10000);
})();
