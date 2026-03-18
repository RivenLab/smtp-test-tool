(() => {
  const serverInput = document.getElementById("server");
  const portInput = document.getElementById("port");
  const resultEl = document.getElementById("result");
  const saveConfigBtn = document.getElementById("save-config-btn");
  const form = document.querySelector('form[action="/smtp/test"]');

  if (serverInput) {
    const serverChips = document.querySelectorAll(".server-chip");
    const syncActiveServerChip = () => {
      const currentServer = String(serverInput.value || "").toLowerCase();
      serverChips.forEach((chip) => {
        chip.classList.toggle("active", (chip.dataset.server || "").toLowerCase() === currentServer);
      });
    };

    serverChips.forEach((chip) => {
      chip.addEventListener("click", () => {
        serverInput.value = chip.dataset.server || "";
        serverInput.dispatchEvent(new Event("input", { bubbles: true }));
        serverInput.focus();
        syncActiveServerChip();
      });
    });

    serverInput.addEventListener("input", syncActiveServerChip);
    syncActiveServerChip();
  }

  if (portInput) {
    const chips = document.querySelectorAll(".port-chip");
    const syncActiveChip = () => {
      const currentPort = String(portInput.value || "");
      chips.forEach((chip) => {
        chip.classList.toggle("active", chip.dataset.port === currentPort);
      });
    };

    chips.forEach((chip) => {
      chip.addEventListener("click", () => {
        portInput.value = chip.dataset.port || "";
        portInput.dispatchEvent(new Event("input", { bubbles: true }));
        portInput.focus();
        syncActiveChip();
      });
    });

    portInput.addEventListener("input", syncActiveChip);
    syncActiveChip();
  }

  if (!form || !resultEl) return;

  const renderStatus = (type, message) => {
    resultEl.classList.remove("result-info", "result-success", "result-error");
    resultEl.classList.add(`result-${type}`);
    resultEl.textContent = message;
  };

  const streamSmtpDebug = async (formData) => {
    const response = await fetch("/smtp/test/stream", {
      method: "POST",
      body: formData,
      headers: {
        Accept: "text/plain",
      },
    });

    if (!response.body) {
      throw new Error("Streaming unsupported by browser.");
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let done = false;
    let buffer = "";
    let hasOutput = false;
    let resultType = response.ok ? "success" : "error";

    while (!done) {
      const chunk = await reader.read();
      done = chunk.done;
      buffer += decoder.decode(chunk.value || new Uint8Array(), { stream: !done });

      let newlineIndex = buffer.indexOf("\n");
      while (newlineIndex >= 0) {
        const line = buffer.slice(0, newlineIndex).replace(/\r$/, "");
        buffer = buffer.slice(newlineIndex + 1);

        if (line.startsWith("__RESULT__|")) {
          resultType = line.split("|")[1] || resultType;
        } else if (line !== "") {
          hasOutput = true;
          resultEl.textContent += (resultEl.textContent ? "\n" : "") + line;
          resultEl.scrollTop = resultEl.scrollHeight;
        }

        newlineIndex = buffer.indexOf("\n");
      }
    }

    if (!hasOutput) {
      resultEl.textContent = "No output received.";
      resultType = "error";
    }

    resultEl.classList.remove("result-info", "result-success", "result-error");
    resultEl.classList.add(`result-${resultType}`);
  };

  form.addEventListener("submit", async (event) => {
    event.preventDefault();

    renderStatus("info", "Starting SMTP test...");

    try {
      const formData = new FormData(form);
      await streamSmtpDebug(formData);
    } catch (error) {
      renderStatus("error", `ERROR: ${error.message}`);
    }
  });

  if (saveConfigBtn) {
    saveConfigBtn.addEventListener("click", async () => {
      renderStatus("info", "Saving configuration...");
      saveConfigBtn.disabled = true;

      try {
        const formData = new FormData(form);
        const response = await fetch("/smtp/configs/save", {
          method: "POST",
          body: formData,
          headers: {
            Accept: "application/json",
          },
        });

        const payload = await response.json().catch(() => ({}));
        if (!response.ok || !payload.ok) {
          throw new Error(payload.message || "Unable to save config.");
        }

        renderStatus("success", payload.message || "Configuration saved.");
        if (payload.config_id) {
          window.setTimeout(() => {
            window.location.href = `/?tab=smtp&config_id=${encodeURIComponent(String(payload.config_id))}`;
          }, 450);
        }
      } catch (error) {
        renderStatus("error", `ERROR: ${error.message}`);
      } finally {
        saveConfigBtn.disabled = false;
      }
    });
  }
})();
