(() => {
  let focusKey = "";
  let feedbackMessage = "";
  let fallbackFocusElement = null;

  document.addEventListener("htmx:beforeRequest", (event) => {
    const element = event.detail.elt;
    focusKey = element.dataset.focusKey || "";
    feedbackMessage = element.dataset.feedbackMessage || "";
    const removableItem = element.closest(".watchlist-item");
    if (removableItem) {
      fallbackFocusElement = (
        removableItem.nextElementSibling?.querySelector("button, a")
        || removableItem.previousElementSibling?.querySelector("button, a")
        || document.querySelector(".watchlist-settings-link")
      );
    }
  });

  document.addEventListener("htmx:afterSwap", () => {
    if (focusKey) {
      const target = document.querySelector(
        `[data-focus-key="${CSS.escape(focusKey)}"]`,
      );
      if (target) {
        target.focus();
      } else if (fallbackFocusElement instanceof HTMLElement) {
        fallbackFocusElement.focus();
      }
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
    fallbackFocusElement = null;
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

  document.querySelectorAll(".copy-source-btn").forEach((button) => {
    if (!(button instanceof HTMLButtonElement)) {
      return;
    }
    let resetTimer = null;
    button.addEventListener("click", () => {
      const value = button.dataset.copyValue || "";
      const copiedLabel = button.dataset.copiedLabel || button.textContent;
      const originalLabel = button.dataset.copyLabel || button.textContent;
      navigator.clipboard.writeText(value).then(() => {
        window.clearTimeout(resetTimer);
        button.textContent = copiedLabel;
        resetTimer = window.setTimeout(() => {
          button.textContent = originalLabel;
        }, 1500);
      }).catch(() => {
        // Clipboard access can be denied (e.g. insecure context) -- leave the label as-is.
      });
    });
  });

  document.querySelectorAll("[data-source-test-form]").forEach((form) => {
    form.addEventListener("submit", () => {
      const button = form.querySelector("button[type='submit']");
      form.setAttribute("aria-busy", "true");
      if (button instanceof HTMLButtonElement) {
        button.disabled = true;
        button.classList.add("is-loading");
      }
    });
  });

  document.querySelectorAll("[data-schedule-builder]").forEach((builder) => {
    // The radio group's field name differs per surface (Email Group delivery uses
    // "schedule_mode", Channel fetching "fetch_schedule_mode"), so the builder names its own.
    const modeField = builder.dataset.scheduleBuilder || "schedule_mode";
    const modeInputs = [
      ...builder.querySelectorAll(`input[name="${modeField}"]`),
    ];
    const panels = [...builder.querySelectorAll("[data-schedule-panel]")];
    const syncScheduleFields = () => {
      const selectedMode = modeInputs.find((input) => input.checked)?.value || "interval";
      panels.forEach((panel) => {
        panel.hidden = panel.dataset.schedulePanel !== selectedMode;
      });
    };
    modeInputs.forEach((input) => {
      input.addEventListener("change", syncScheduleFields);
    });
    syncScheduleFields();
  });

  const shouldRestoreDraft = new URLSearchParams(window.location.search).get("reauth") === "1";
  document.querySelectorAll("form[data-preserve-draft]").forEach((form) => {
    if (!(form instanceof HTMLFormElement)) {
      return;
    }
    const storageKey = `beehive:draft:${window.location.pathname}:${form.action}`;
    const saveDraft = () => {
      const values = {};
      [...form.elements].forEach((control) => {
        if (
          !(control instanceof HTMLInputElement
            || control instanceof HTMLTextAreaElement
            || control instanceof HTMLSelectElement)
          || !control.name
          || ["csrf_token", "password", "next"].includes(control.name)
          || control instanceof HTMLInputElement && control.type === "file"
        ) {
          return;
        }
        if (
          control instanceof HTMLInputElement
          && ["checkbox", "radio"].includes(control.type)
        ) {
          if (!Object.hasOwn(values, control.name)) {
            values[control.name] = [];
          }
          if (control.checked) {
            values[control.name].push(control.value);
          }
          return;
        }
        const value = control.value;
        if (Object.hasOwn(values, control.name)) {
          values[control.name] = (
            Array.isArray(values[control.name])
              ? [...values[control.name], value]
              : [values[control.name], value]
          );
        } else {
          values[control.name] = value;
        }
      });
      try {
        window.sessionStorage.setItem(storageKey, JSON.stringify(values));
      } catch (error) {
        console.warn("Could not preserve the form draft", error);
      }
    };

    if (shouldRestoreDraft) {
      try {
        const stored = window.sessionStorage.getItem(storageKey);
        const values = stored ? JSON.parse(stored) : null;
        if (values && typeof values === "object") {
          [...form.elements].forEach((control) => {
            if (
              !(control instanceof HTMLInputElement
                || control instanceof HTMLTextAreaElement
                || control instanceof HTMLSelectElement)
              || !control.name
              || !Object.hasOwn(values, control.name)
            ) {
              return;
            }
            const storedValue = values[control.name];
            if (
              control instanceof HTMLInputElement
              && ["checkbox", "radio"].includes(control.type)
            ) {
              const selected = Array.isArray(storedValue) ? storedValue : [storedValue];
              control.checked = selected.includes(control.value);
            } else if (typeof storedValue === "string") {
              control.value = storedValue;
            }
            control.dispatchEvent(new Event("input", { bubbles: true }));
            control.dispatchEvent(new Event("change", { bubbles: true }));
          });
        }
      } catch (error) {
        console.warn("Could not restore the form draft", error);
      }
    }
    form.addEventListener("input", saveDraft);
    form.addEventListener("change", saveDraft);
  });

  // Admin forms mark each parameter row the Owner has changed but not saved, and count them in
  // the sticky save bar, so leaving a long settings page never silently drops an edit.
  document.querySelectorAll("form[data-dirty-track]").forEach((form) => {
    if (!(form instanceof HTMLFormElement)) {
      return;
    }
    const status = form.querySelector("[data-dirty-status]");
    const reset = form.querySelector("[data-dirty-reset]");
    const template = status instanceof HTMLElement ? status.dataset.template || "__COUNT__" : "__COUNT__";
    // After a rejected save the page shows the submitted values as its defaults, so nothing
    // looks changed. The form is still unsaved: say so, and let "discard" reload the saved page.
    const forced = form.hasAttribute("data-dirty-force");
    const draftKey = `beehive:draft:${window.location.pathname}:${form.action}`;
    const controls = [...form.elements].filter((control) => (
      (control instanceof HTMLInputElement
        || control instanceof HTMLTextAreaElement
        || control instanceof HTMLSelectElement)
      && control.name
      && control.type !== "hidden"
    ));
    const isChanged = (control) => {
      if (control instanceof HTMLInputElement && ["checkbox", "radio"].includes(control.type)) {
        return control.checked !== control.defaultChecked;
      }
      if (control instanceof HTMLSelectElement) {
        return [...control.options].some((option) => option.selected !== option.defaultSelected);
      }
      return control.value !== control.defaultValue;
    };
    // A change inside a hidden block (another source type, the other schedule mode) does not
    // apply to the option the Owner has chosen, so it is not counted.
    const isShown = (control) => typeof control.checkVisibility !== "function" || control.checkVisibility();
    const syncDirtyState = () => {
      const dirtyRows = new Set();
      controls.forEach((control) => {
        const row = control.closest(".param") || control.closest("[data-param-group]");
        if (row && isChanged(control) && isShown(control)) {
          dirtyRows.add(row);
        }
      });
      form.querySelectorAll("[data-dirty]").forEach((row) => {
        if (!dirtyRows.has(row)) {
          delete row.dataset.dirty;
        }
      });
      dirtyRows.forEach((row) => {
        row.dataset.dirty = "";
      });
      const count = dirtyRows.size;
      if (status instanceof HTMLElement) {
        status.hidden = count === 0 && !forced;
        status.textContent = count === 0 && forced
          ? status.dataset.forceMessage || ""
          : template.replace("__COUNT__", String(count));
      }
      if (reset instanceof HTMLElement) {
        reset.hidden = count === 0 && !forced;
      }
    };
    if (forced && reset instanceof HTMLButtonElement) {
      reset.addEventListener("click", (event) => {
        event.preventDefault();
        try {
          window.sessionStorage.removeItem(draftKey);
        } catch (error) {
          console.warn("Could not clear the form draft", error);
        }
        window.location.assign(form.getAttribute("action") || window.location.pathname);
      });
    }
    form.addEventListener("input", syncDirtyState);
    form.addEventListener("change", syncDirtyState);
    // "reset" fires before the browser restores the defaults, so read the state a tick later, and
    // let the schedule builders re-show the panel for the restored mode.
    form.addEventListener("reset", () => window.setTimeout(() => {
      form.querySelectorAll("[data-schedule-builder] input[type='radio']:checked").forEach((radio) => {
        radio.dispatchEvent(new Event("change", { bubbles: true }));
      });
      // Also refreshes a preserved draft, so a re-login cannot bring discarded edits back.
      form.dispatchEvent(new Event("change", { bubbles: true }));
      syncDirtyState();
    }, 0));
    syncDirtyState();
  });

  // Long one-line values (URLs, queries, address lists) are wrapping textareas, so the whole
  // value stays visible. They still behave like a text input: Enter submits the form and pasted
  // line breaks are dropped. Browsers without CSS field-sizing get their height from here.
  const singleLineFields = [...document.querySelectorAll("textarea[data-single-line]")];
  if (singleLineFields.length > 0) {
    const growsNatively = Boolean(window.CSS && window.CSS.supports("field-sizing", "content"));
    const isSingleLine = (target) => (
      target instanceof HTMLTextAreaElement && target.hasAttribute("data-single-line")
    );
    const fitHeight = (field) => {
      if (growsNatively || field.getClientRects().length === 0) {
        return;
      }
      const style = window.getComputedStyle(field);
      field.style.height = "auto";
      field.style.height = `${field.scrollHeight
        + parseFloat(style.borderTopWidth) + parseFloat(style.borderBottomWidth)}px`;
    };
    document.addEventListener("keydown", (event) => {
      if (!isSingleLine(event.target) || event.key !== "Enter" || event.isComposing || event.keyCode === 229) {
        return;
      }
      event.preventDefault();
      const form = event.target.form;
      const submitter = form && [...form.elements].find((control) => (
        (control instanceof HTMLButtonElement || control instanceof HTMLInputElement)
        && control.type === "submit"
      ));
      if (form && !(submitter && submitter.disabled)) {
        form.requestSubmit(submitter || undefined);
      }
    });
    document.addEventListener("input", (event) => {
      const field = event.target;
      if (!isSingleLine(field)) {
        return;
      }
      if (/[\r\n]/.test(field.value)) {
        const caret = field.value.slice(0, field.selectionStart ?? field.value.length).replace(/[\r\n]/g, "").length;
        field.value = field.value.replace(/[\r\n]/g, "");
        field.setSelectionRange(caret, caret);
      }
      fitHeight(field);
    });
    if (!growsNatively) {
      document.addEventListener("reset", (event) => window.setTimeout(() => {
        if (event.target instanceof HTMLFormElement) {
          event.target.querySelectorAll("textarea[data-single-line]").forEach(fitHeight);
        }
      }, 0));
      // Refit when a field's width changes, including when a hidden source type is shown.
      const widths = new WeakMap();
      const observer = "ResizeObserver" in window ? new ResizeObserver((entries) => {
        entries.forEach(({ target }) => {
          if (widths.get(target) !== target.clientWidth) {
            widths.set(target, target.clientWidth);
            fitHeight(target);
          }
        });
      }) : null;
      singleLineFields.forEach((field) => {
        fitHeight(field);
        if (observer) {
          observer.observe(field);
        }
      });
    }
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
