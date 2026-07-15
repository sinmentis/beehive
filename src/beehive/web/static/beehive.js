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

  const channelForm = document.querySelector(".channel-bulk-form");
  if (channelForm instanceof HTMLFormElement) {
    const selectAll = channelForm.querySelector("[data-channel-select-all]");
    const channelCheckboxes = [
      ...channelForm.querySelectorAll("[data-channel-checkbox]"),
    ];
    const countLabel = channelForm.querySelector(".channel-selection-count");
    const submitButton = channelForm.querySelector("button[type='submit']");
    const countTemplate = channelForm.dataset.selectedTemplate || "__COUNT__";

    const syncChannelSelection = () => {
      const selectedCount = channelCheckboxes.filter((checkbox) => checkbox.checked).length;
      if (countLabel) {
        countLabel.textContent = countTemplate.replace("__COUNT__", String(selectedCount));
      }
      if (submitButton instanceof HTMLButtonElement) {
        submitButton.disabled = selectedCount === 0;
      }
      if (selectAll instanceof HTMLInputElement) {
        selectAll.checked = selectedCount === channelCheckboxes.length;
        selectAll.indeterminate = selectedCount > 0 && selectedCount < channelCheckboxes.length;
      }
    };

    selectAll?.addEventListener("change", () => {
      channelCheckboxes.forEach((checkbox) => {
        checkbox.checked = selectAll.checked;
      });
      syncChannelSelection();
    });
    channelCheckboxes.forEach((checkbox) => {
      checkbox.addEventListener("change", syncChannelSelection);
    });
    syncChannelSelection();
  }

  const signalTable = document.querySelector(".signal-table");
  if (signalTable instanceof HTMLTableElement) {
    const handles = [...signalTable.querySelectorAll("[data-column-resizer]")];
    const columns = new Map(
      [...signalTable.querySelectorAll("col[data-column-key]")].map((column) => (
        [column.dataset.columnKey, column]
      )),
    );
    const headers = new Map(
      [...signalTable.querySelectorAll("th[data-column-header]")].map((header) => (
        [header.dataset.columnHeader, header]
      )),
    );
    const defaultWidths = new Map();
    let widths = null;
    let drag = null;

    headers.forEach((header, key) => {
      const width = Math.round(header.getBoundingClientRect().width);
      if (width > 0) {
        defaultWidths.set(key, width);
      }
    });

    const syncTableWidth = () => {
      if (!widths) {
        return;
      }
      const contentWidth = [...widths.values()].reduce((total, width) => total + width, 0);
      signalTable.style.width = `${contentWidth}px`;
    };

    const freezeVisibleWidths = () => {
      if (widths) {
        return;
      }
      widths = new Map();
      headers.forEach((header, key) => {
        const width = Math.round(header.getBoundingClientRect().width);
        const column = columns.get(key);
        if (width > 0 && column instanceof HTMLTableColElement) {
          widths.set(key, width);
          column.style.width = `${width}px`;
        }
      });
      syncTableWidth();
    };

    const setColumnWidth = (handle, width) => {
      const key = handle.dataset.columnResizer;
      const column = columns.get(key);
      const minimum = Number(handle.dataset.minWidth) || 40;
      if (
        !key
        || !(column instanceof HTMLTableColElement)
        || !widths
        || !Number.isFinite(width)
      ) {
        return;
      }
      const nextWidth = Math.max(minimum, Math.min(1600, Math.round(width)));
      widths.set(key, nextWidth);
      column.style.width = `${nextWidth}px`;
      handle.setAttribute("aria-valuenow", String(nextWidth));
      syncTableWidth();
    };

    const finishDrag = (handle) => {
      if (!drag) {
        return;
      }
      if (handle.hasPointerCapture(drag.pointerId)) {
        handle.releasePointerCapture(drag.pointerId);
      }
      handle.classList.remove("is-active");
      document.documentElement.classList.remove("signal-column-resize-active");
      drag = null;
    };

    handles.forEach((handle) => {
      const key = handle.dataset.columnResizer;
      const initialWidth = defaultWidths.get(key);
      if (initialWidth) {
        handle.setAttribute("aria-valuenow", String(initialWidth));
      }

      handle.addEventListener("pointerdown", (event) => {
        if (event.pointerType === "mouse" && event.button !== 0) {
          return;
        }
        freezeVisibleWidths();
        const startWidth = widths?.get(key);
        if (!startWidth) {
          return;
        }
        drag = {
          pointerId: event.pointerId,
          startX: event.clientX,
          startWidth,
        };
        handle.setPointerCapture(event.pointerId);
        handle.classList.add("is-active");
        document.documentElement.classList.add("signal-column-resize-active");
        event.preventDefault();
      });

      handle.addEventListener("pointermove", (event) => {
        if (!drag || drag.pointerId !== event.pointerId) {
          return;
        }
        setColumnWidth(handle, drag.startWidth + event.clientX - drag.startX);
      });

      handle.addEventListener("pointerup", () => finishDrag(handle));
      handle.addEventListener("pointercancel", () => finishDrag(handle));

      handle.addEventListener("keydown", (event) => {
        if (!["ArrowLeft", "ArrowRight", "Home"].includes(event.key)) {
          return;
        }
        freezeVisibleWidths();
        if (event.key === "Home") {
          setColumnWidth(handle, defaultWidths.get(key));
        } else {
          const direction = event.key === "ArrowRight" ? 1 : -1;
          const step = event.shiftKey ? 24 : 8;
          setColumnWidth(handle, (widths?.get(key) || 0) + direction * step);
        }
        event.preventDefault();
      });

      handle.addEventListener("dblclick", () => {
        freezeVisibleWidths();
        setColumnWidth(handle, defaultWidths.get(key));
      });
    });
  }

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
      const template = selectionStatus.dataset.selectionTemplate || "";
      const replacements = {
        __CHANNEL__: channel,
        __SCORE__: score,
        __SUMMARY__: summary,
      };
      const message = template.replace(
        /__(CHANNEL|SCORE|SUMMARY)__/g,
        (token) => replacements[token] ?? token,
      );
      selectionStatus.textContent = "";
      requestAnimationFrame(() => {
        selectionStatus.textContent = message;
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
