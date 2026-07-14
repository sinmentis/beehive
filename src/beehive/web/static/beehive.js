(() => {
  let focusKey = "";
  let feedbackMessage = "";

  document.addEventListener("htmx:beforeRequest", (event) => {
    const element = event.detail.elt;
    focusKey = element.dataset.focusKey || "";
    feedbackMessage = element.dataset.feedbackMessage || "";
  });

  document.addEventListener("htmx:afterSwap", () => {
    if (focusKey) {
      const target = document.querySelector(
        `[data-focus-key="${CSS.escape(focusKey)}"]`,
      );
      target?.focus();
    }

    if (feedbackMessage) {
      const message = feedbackMessage;
      const status = document.getElementById("feedback-status");
      if (status) {
        status.textContent = "";
        requestAnimationFrame(() => {
          status.textContent = message;
        });
      }
    }

    focusKey = "";
    feedbackMessage = "";
  });

  const search = document.querySelector(".dashboard-search input[type='search']");
  const rows = [...document.querySelectorAll(".signal-row")];
  const selectionStatus = document.getElementById("dashboard-selection-status");
  let selectedIndex = -1;

  if (!search) {
    return;
  }

  const isTyping = (target) => (
    target instanceof HTMLElement
    && (
      target.isContentEditable
      || ["INPUT", "SELECT", "TEXTAREA"].includes(target.tagName)
    )
  );

  const selectRow = (index) => {
    if (rows.length === 0) {
      return;
    }
    selectedIndex = Math.max(0, Math.min(index, rows.length - 1));
    rows.forEach((row, rowIndex) => {
      const selected = rowIndex === selectedIndex;
      row.classList.toggle("is-selected", selected);
      row.tabIndex = selected ? 0 : -1;
    });
    rows[selectedIndex].focus({ preventScroll: true });
    rows[selectedIndex].scrollIntoView({ block: "nearest" });
    if (selectionStatus) {
      const row = rows[selectedIndex];
      const channel = row.querySelector(".signal-channel")?.textContent.trim() || "";
      const score = row.querySelector(".signal-score")?.textContent.trim() || "";
      const summary = (
        row.querySelector(".signal-summary")?.firstChild?.textContent.trim() || ""
      );
      selectionStatus.textContent = "";
      requestAnimationFrame(() => {
        selectionStatus.textContent = `已选择 ${channel}，AI 分数 ${score}，${summary}`;
      });
    }
  };

  const openSelectedRow = () => {
    if (selectedIndex < 0) {
      return;
    }
    const link = rows[selectedIndex].querySelector(".signal-summary");
    if (link instanceof HTMLAnchorElement) {
      window.open(link.href, "_blank", "noopener,noreferrer");
    }
  };

  document.addEventListener("keydown", (event) => {
    if (event.defaultPrevented || event.altKey || event.ctrlKey || event.metaKey) {
      return;
    }

    const key = event.key.toLowerCase();
    if (!isTyping(event.target) && (key === "/" || key === "f")) {
      event.preventDefault();
      search.focus();
      search.select();
      return;
    }

    if (isTyping(event.target)) {
      return;
    }

    if (key === "j" || key === "k") {
      if (rows.length === 0) {
        return;
      }
      event.preventDefault();
      const nextIndex = selectedIndex < 0
        ? (key === "j" ? 0 : rows.length - 1)
        : selectedIndex + (key === "j" ? 1 : -1);
      selectRow(nextIndex);
      return;
    }

    const selectedRowHasFocus = (
      selectedIndex >= 0 && event.target === rows[selectedIndex]
    );
    if (
      (key === "o" && selectedIndex >= 0)
      || (key === "enter" && selectedRowHasFocus)
    ) {
      event.preventDefault();
      openSelectedRow();
    }
  });
})();
