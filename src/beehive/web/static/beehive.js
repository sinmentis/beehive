(() => {
  let focusKey = "";
  let feedbackMessage = "";
  let fallbackFocusSelector = "";
  let refocusSlot = null;
  let noticeReturnFocus = null;
  let readingWidth = document.body.clientWidth;
  const narrowReading = () => readingWidth <= 880;
  const preparedDisclosures = new WeakSet();

  const revealCurrentChapter = () => {
    const rail = document.querySelector(".page-reading .toc");
    const current = rail?.querySelector(":scope > li > a[aria-current]");
    if (!narrowReading() || !rail || !current) return;
    const bounds = rail.getBoundingClientRect();
    const active = current.getBoundingClientRect();
    if (active.left < bounds.left || active.right > bounds.right) {
      rail.scrollLeft += active.left - bounds.left - Math.max(0, (bounds.width - active.width) / 2);
    }
  };

  const prepareReadingControls = () => {
    document.querySelectorAll("details[data-responsive-disclosure]").forEach((details) => {
      if (preparedDisclosures.has(details)) return;
      preparedDisclosures.add(details);
      details.open = !narrowReading();
    });
    revealCurrentChapter();
  };
  prepareReadingControls();
  new ResizeObserver(() => {
    const width = document.body.clientWidth;
    if (width === readingWidth) return;
    const wasNarrow = narrowReading();
    readingWidth = width;
    if (wasNarrow !== narrowReading()) {
      document.querySelectorAll("details[data-responsive-disclosure]").forEach((details) => {
        details.open = !narrowReading();
        if (!details.open && details.contains(document.activeElement)) {
          details.querySelector("summary")?.focus({ preventScroll: true });
        }
      });
    }
    revealCurrentChapter();
  }).observe(document.body);
  window.addEventListener("pageshow", revealCurrentChapter);
  document.fonts?.ready.then(revealCurrentChapter);
  document.addEventListener("click", (event) => {
    const menu = document.querySelector(".reading-owner-menu[open]");
    if (narrowReading() && menu && !menu.contains(event.target)) menu.open = false;
    const dismiss = event.target.closest("[data-dismiss-reading-action]");
    if (!dismiss) return;
    event.preventDefault();
    document.getElementById("reading-action-notice")?.replaceChildren();
    if (noticeReturnFocus?.isConnected) noticeReturnFocus.focus({ preventScroll: true });
  });
  document.addEventListener("keydown", (event) => {
    const menu = document.querySelector(".reading-owner-menu[open]");
    if (event.key === "Escape" && narrowReading() && menu) {
      menu.open = false;
      menu.querySelector("summary")?.focus({ preventScroll: true });
      event.preventDefault();
      event.stopImmediatePropagation();
    }
  });

  document.addEventListener("htmx:beforeRequest", (event) => {
    // A form posted by htmx is triggered by the form, but the focus key and the announcement
    // belong to the button that submitted it.
    const submitter = event.detail.requestConfig?.triggeringEvent?.submitter;
    const element = submitter instanceof HTMLElement && event.detail.elt.contains(submitter)
      ? submitter
      : event.detail.elt;
    focusKey = element.dataset.focusKey || "";
    feedbackMessage = element.dataset.feedbackMessage || "";
    // Stopping a watch re-renders the whole list, so remember where focus should land by id:
    // the next lot, else the previous one, else the page's settings link.
    const removableItem = element.closest(".watchlist-item");
    if (removableItem) {
      const neighbour = removableItem.nextElementSibling || removableItem.previousElementSibling;
      fallbackFocusSelector = neighbour?.id
        ? `#${CSS.escape(neighbour.id)} .lot-title`
        : ".watchlist-settings-link";
    }
    // A read toggle re-renders its section, where the next story may now sit in this row's
    // place, so focus returns to the same slot rather than following the story.
    const slotRow = element.closest("[data-refocus-slot] tr.kb-row");
    const slotRegion = slotRow?.closest("[data-refocus-slot]");
    refocusSlot = slotRow && slotRegion?.id
      ? { region: slotRegion.id, index: [...slotRegion.querySelectorAll("tr.kb-row")].indexOf(slotRow) }
      : null;
  });

  const announce = (message) => {
    const status = document.getElementById("feedback-status");
    if (message && status) {
      status.textContent = "";
      requestAnimationFrame(() => {
        status.textContent = message;
      });
    }
  };

  document.addEventListener("htmx:afterSwap", () => {
    prepareReadingControls();
    if (refocusSlot) {
      const region = document.getElementById(refocusSlot.region);
      const rows = region ? [...region.querySelectorAll("tr.kb-row")] : [];
      const row = rows[Math.min(refocusSlot.index, rows.length - 1)];
      // With no row left (the last unread story was just read), focus the region's first link
      // or its empty-state message, so keyboard focus never drops to the page.
      const target = row?.querySelector("button.rd")
        || region?.querySelector("a[href], [data-refocus-fallback]");
      if (target instanceof HTMLElement) {
        target.focus();
      }
    }
    if (focusKey) {
      const target = document.querySelector(
        `[data-focus-key="${CSS.escape(focusKey)}"]`,
      );
      const fallback = fallbackFocusSelector
        ? document.querySelector(fallbackFocusSelector)
        : null;
      if (target) {
        target.focus();
      } else if (fallback instanceof HTMLElement) {
        fallback.focus();
      }
    }
    announce(feedbackMessage);
    if (document.activeElement instanceof HTMLElement
        && !document.activeElement.closest("#reading-action-notice")) {
      noticeReturnFocus = document.activeElement;
    }

    focusKey = "";
    feedbackMessage = "";
    fallbackFocusSelector = "";
    refocusSlot = null;
  });

  const channelForm = document.querySelector(".channel-bulk-form");
  if (channelForm instanceof HTMLFormElement) {
    const selectAll = channelForm.querySelector("[data-channel-select-all]");
    const channelCheckboxes = [
      ...channelForm.querySelectorAll("[data-channel-checkbox]"),
    ].filter(checkbox => !checkbox.disabled);
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

  document.querySelectorAll("select[data-source-type]").forEach((select) => {
    const form = select.closest("form");
    if (!form) return;
    const syncSourcePanels = () => {
      form.querySelectorAll("[data-source-panel]").forEach((panel) => {
        const active = panel.dataset.sourcePanel === select.value;
        panel.hidden = !active;
        if (panel instanceof HTMLFieldSetElement) panel.disabled = !active;
        panel.querySelectorAll("[data-source-required] input, [data-source-required] textarea, [data-source-required] select").forEach((field) => {
          field.required = active;
        });
      });
    };
    select.addEventListener("change", syncSourcePanels);
    form.addEventListener("reset", () => window.setTimeout(syncSourcePanels, 0));
    syncSourcePanels();
  });

  document.querySelectorAll("[data-error-focus]").forEach((link) => {
    link.addEventListener("click", (event) => {
      const id = (link.dataset.errorFocus || link.getAttribute("href") || "").replace(/^#/, "");
      const field = document.getElementById(id);
      if (field instanceof HTMLElement) {
        event.preventDefault();
        field.focus();
        field.scrollIntoView({ block: "center" });
      }
    });
  });

  document.querySelectorAll("[data-delivery-builder]").forEach((builder) => {
    const radios = [...builder.querySelectorAll("[name='delivery_mode']")];
    const syncDelivery = () => {
      const mode = radios.find((radio) => radio.checked)?.value || "none";
      builder.querySelectorAll("[data-delivery-panel]").forEach((panel) => {
        const active = panel.dataset.deliveryPanel === mode;
        panel.hidden = !active;
        panel.querySelectorAll("input, select, textarea").forEach((field) => {
          field.disabled = !active;
        });
        if (mode !== "new") {
          builder.closest("form")?.querySelectorAll("[data-email-schedule-summary]").forEach(summary => {
            summary.hidden = true;
          });
        }
      });
    };
    radios.forEach((radio) => radio.addEventListener("change", syncDelivery));
    builder.closest("form")?.addEventListener("reset", () => window.setTimeout(syncDelivery, 0));
    syncDelivery();
  });

  const channelWorkspace = document.querySelector("[data-channel-workspace]");
  if (channelWorkspace) {
    const legacySections = { "#sources": "sources", "#settings": "settings", "#danger": "settings" };
    const legacy = legacySections[window.location.hash];
    if (legacy) {
      const address = new URL(window.location.href);
      address.searchParams.set("section", legacy);
      if (address.hash !== "#danger") address.hash = "";
      if (address.href !== window.location.href) {
        window.location.replace(address);
        return;
      }
    }
  }

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

  const namedDraftKey = (name) => `beehive:draft:${name}`;
  const draftKeyFor = (form) => form.dataset.draftKey
    ? namedDraftKey(form.dataset.draftKey)
    : `beehive:draft:${window.location.pathname}:${form.action}`;
  const isTaskAdmin = document.body.classList.contains("page-admin");
  const isDraftPage = isTaskAdmin || document.body.classList.contains("page-workspace");
  const pendingDraftKey = "beehive:draft:submitted";
  const previewDraftKey = "beehive:draft:previewed";
  let previewedDraft = "";
  try {
    previewedDraft = window.sessionStorage.getItem(previewDraftKey) || "";
    window.sessionStorage.removeItem(previewDraftKey);
    const pending = window.sessionStorage.getItem(pendingDraftKey);
    if (pending && document.querySelector("[data-save-confirmation]")
      && !document.querySelector("form[data-dirty-force]")) {
      window.sessionStorage.removeItem(pending);
    }
    window.sessionStorage.removeItem(pendingDraftKey);
  } catch (error) {
    console.warn("Could not finish saving the form draft", error);
  }

  const readDraftValues = (form, defaults = false) => {
    const values = {};
    [...form.elements].forEach((control) => {
      if (
        !(control instanceof HTMLInputElement
          || control instanceof HTMLTextAreaElement
          || control instanceof HTMLSelectElement)
        || !control.name
        || ["csrf_token", "password", "next"].includes(control.name)
        || control instanceof HTMLInputElement && ["hidden", "password", "file"].includes(control.type)
      ) {
        return;
      }
      if (control instanceof HTMLInputElement && ["checkbox", "radio"].includes(control.type)) {
        values[control.name] ||= [];
        if (defaults ? control.defaultChecked : control.checked) {
          values[control.name].push(control.value);
        }
        return;
      }
      const value = defaults
        ? control instanceof HTMLSelectElement
          ? [...control.options].find((option) => option.defaultSelected)?.value || control.value
          : control.defaultValue
        : control.value;
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
    return values;
  };

  document.querySelectorAll("form[data-preserve-draft]").forEach((form) => {
    if (!(form instanceof HTMLFormElement)) {
      return;
    }
    const storageKey = draftKeyFor(form);
    let baseline = readDraftValues(form, true);
    const restoreValues = (values) => {
      [...form.elements].forEach((control) => {
        if (
          !(control instanceof HTMLInputElement
            || control instanceof HTMLTextAreaElement
            || control instanceof HTMLSelectElement)
          || !control.name || !Object.hasOwn(values, control.name)
          || control instanceof HTMLInputElement && ["hidden", "password", "file"].includes(control.type)
        ) return;
        const value = values[control.name];
        if (control instanceof HTMLInputElement && ["checkbox", "radio"].includes(control.type)) {
          control.checked = (Array.isArray(value) ? value : [value]).includes(control.value);
        } else if (typeof value === "string") {
          control.value = value;
        }
        control.dispatchEvent(new Event("input", { bubbles: true }));
        control.dispatchEvent(new Event("change", { bubbles: true }));
      });
    };
    const saveDraft = () => {
      try {
        const values = readDraftValues(form);
        if (JSON.stringify(values) === JSON.stringify(baseline)
          && !form.hasAttribute("data-dirty-force")) {
          window.sessionStorage.removeItem(storageKey);
        } else {
          window.sessionStorage.setItem(storageKey, JSON.stringify({ baseline, values }));
        }
      } catch (error) {
        console.warn("Could not preserve the form draft", error);
      }
    };

    const reauth = new URLSearchParams(window.location.search).get("reauth") === "1";
    if (!form.closest("[hidden]") && (isDraftPage || reauth)) {
      try {
        const stored = window.sessionStorage.getItem(storageKey);
        const draft = stored ? JSON.parse(stored) : null;
        const matchesBaseline = draft?.baseline
          && JSON.stringify(draft.baseline) === JSON.stringify(baseline);
        const returnedSubmission = draft?.values && (
          form.hasAttribute("data-dirty-force") || previewedDraft === storageKey
        )
          && JSON.stringify(draft.values) === JSON.stringify(readDraftValues(form));
        const values = matchesBaseline || returnedSubmission
          ? draft.values
          : reauth && draft && !draft.baseline ? draft : null;
        if (returnedSubmission) {
          baseline = draft.baseline;
          form.setAttribute("data-dirty-force", "");
        }
        if (values && typeof values === "object") {
          restoreValues(values);
          const message = form.dataset.draftRestored;
          if (message) {
            const notice = document.createElement("p");
            notice.className = "draft-notice";
            notice.setAttribute("role", "status");
            notice.textContent = message;
            form.prepend(notice);
          }
        } else if (draft?.values) {
          const messages = form.querySelector("[data-draft-messages]");
          if (messages instanceof HTMLElement) {
            const notice = document.createElement("div");
            notice.className = "draft-notice";
            notice.setAttribute("role", "status");
            const text = document.createElement("p");
            text.textContent = messages.dataset.conflict || "";
            const restore = document.createElement("button");
            restore.type = "button";
            restore.className = "btn btn-sm";
            restore.textContent = messages.dataset.restore || "";
            restore.addEventListener("click", () => {
              restoreValues(draft.values);
              saveDraft();
              notice.remove();
            });
            const discard = document.createElement("button");
            discard.type = "button";
            discard.className = "btn btn-sm";
            discard.textContent = messages.dataset.discard || "";
            discard.addEventListener("click", () => {
              window.sessionStorage.removeItem(storageKey);
              notice.remove();
            });
            notice.append(text, restore, discard);
            form.prepend(notice);
          }
        }
      } catch (error) {
        console.warn("Could not restore the form draft", error);
      }
    }
    form.addEventListener("input", saveDraft);
    form.addEventListener("change", saveDraft);
    if (form.hasAttribute("data-dirty-force")) saveDraft();
    form.addEventListener("submit", (event) => {
      saveDraft();
      form.dataset.submitting = "true";
      const submitter = event.submitter;
      const isPreview = form.hasAttribute("data-keep-draft") || submitter instanceof HTMLElement && (
        submitter.hasAttribute("data-keep-draft")
        || ["test", "preview"].includes(submitter.getAttribute("value"))
        || submitter.hasAttribute("formaction")
      );
      if (!isDraftPage) return;
      if (isPreview) {
        try {
          window.sessionStorage.setItem(previewDraftKey, storageKey);
        } catch (error) {
          console.warn("Could not mark the previewed draft", error);
        }
      } else {
        try {
          window.sessionStorage.setItem(pendingDraftKey, storageKey);
        } catch (error) {
          console.warn("Could not mark the submitted draft", error);
        }
      }
    });
  });

  document.querySelectorAll("form[data-submit-draft]").forEach((form) => {
    form.addEventListener("submit", () => {
      try {
        window.sessionStorage.setItem(pendingDraftKey, namedDraftKey(form.dataset.submitDraft));
      } catch (error) {
        console.warn("Could not mark the submitted draft", error);
      }
    });
  });

  const syncRuleSaveButtons = () => {
    document.querySelectorAll("[data-save-rules-for]").forEach((button) => {
      const form = document.getElementById(button.dataset.saveRulesFor);
      button.disabled = !form || form.dataset.dirty !== "true";
    });
    document.querySelectorAll("[data-requires-saved-notice]").forEach((notice) => {
      const form = document.getElementById(notice.dataset.requiresSavedNotice);
      notice.hidden = !form || form.dataset.dirty !== "true";
    });
    const profile = document.getElementById("channel-profile");
    document.querySelectorAll("[data-preview-stale]").forEach((notice) => {
      notice.hidden = !profile || profile.value.trim() === notice.dataset.previewProfile.trim();
    });
  };

  // Opted-in forms keep their dirty state separate from values returned after a rejected save.
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
    const draftKey = draftKeyFor(form);
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
      form.dataset.dirty = String(forced || controls.some((control) => (
        isChanged(control) && isShown(control) && !control.disabled
      )));
      if (form.hasAttribute("data-save-when-dirty")) {
        const save = form.querySelector(".savebar .btn-primary");
        if (save instanceof HTMLButtonElement) save.disabled = form.dataset.dirty !== "true";
      }
      document.querySelectorAll("[data-requires-saved-form]").forEach((button) => {
        if (button instanceof HTMLButtonElement && button.dataset.requiresSavedForm === form.id) {
          button.disabled = form.dataset.dirty === "true" || button.hasAttribute("data-operation-busy");
        }
      });
      syncRuleSaveButtons();
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
        form.dataset.submitting = "true";
        try {
          window.sessionStorage.removeItem(draftKey);
        } catch (error) {
          console.warn("Could not clear the form draft", error);
        }
        window.location.assign(form.dataset.resetUrl || form.getAttribute("action") || window.location.pathname);
      });
    }
    const changed = () => {
      delete form.dataset.submitting;
      syncDirtyState();
    };
    form.addEventListener("input", changed);
    form.addEventListener("change", changed);
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

  window.addEventListener("beforeunload", (event) => {
    if (!isDraftPage) return;
    const dirty = [...document.querySelectorAll("form[data-dirty-track]")].some((form) => (
      form.dataset.dirty === "true" && !form.dataset.submitting && !form.closest("[hidden]")
    ));
    if (dirty) {
      event.preventDefault();
      event.returnValue = "";
    }
  });
  window.addEventListener("pageshow", (event) => {
    if (event.persisted) {
      document.querySelectorAll("form[data-dirty-track]").forEach((form) => {
        delete form.dataset.submitting;
        form.dispatchEvent(new Event("change", { bubbles: true }));
      });
    }
  });

  document.addEventListener("focusin", (event) => {
    const field = event.target;
    if (!isDraftPage || !(field instanceof HTMLInputElement
      || field instanceof HTMLTextAreaElement || field instanceof HTMLSelectElement)) return;
    requestAnimationFrame(() => {
      if (document.activeElement !== field) return;
      const bar = field.closest("form")?.querySelector(".savebar");
      if (!bar || !bar.getClientRects().length) return;
      const inputBox = field.getBoundingClientRect();
      const barBox = bar.getBoundingClientRect();
      if (barBox.top < window.innerHeight && inputBox.bottom > barBox.top
        && inputBox.top < window.innerHeight) {
        field.scrollIntoView({ block: "center" });
      }
    });
  });
  const invalidField = document.querySelector("form [aria-invalid='true']:not([disabled])");
  if (invalidField instanceof HTMLElement && !invalidField.closest("[hidden]")) {
    invalidField.focus();
  } else {
    const sourcePreview = document.getElementById("source-preview");
    if (sourcePreview) {
      sourcePreview.focus();
      sourcePreview.scrollIntoView({ block: "start" });
    }
  }

  const syncScorePreview = () => {
    const cutoff = Number(document.getElementById("minimum-score")?.value || 0);
    document.querySelectorAll(".rule-operation").forEach((operation) => {
      const samples = [...operation.querySelectorAll("[data-preview-score]")];
      const output = operation.querySelector("[data-score-preview-count]");
      if (output) {
        const matched = samples.filter(sample => Number(sample.dataset.previewScore) >= cutoff).length;
        output.textContent = (output.dataset.template || "")
          .replace("__MATCHED__", String(matched)).replace("__COUNT__", String(samples.length));
      }
    });
  };
  document.getElementById("minimum-score")?.addEventListener("input", syncScorePreview);
  syncScorePreview();

  document.querySelectorAll("[data-task-poll]").forEach((operation) => {
    let failures = 0;
    let stopped = false;
    const poll = async () => {
      if (stopped || !operation.isConnected) return;
      if (document.hidden || operation.closest("[hidden]")) {
        window.setTimeout(poll, 5000);
        return;
      }
      try {
        const response = await fetch(operation.dataset.taskPoll, {
          credentials: "same-origin", headers: { Accept: "application/json" },
        });
        if (!response.ok) throw new Error("Progress request failed");
        const result = await response.json();
        failures = 0;
        const progress = operation.querySelector("[data-task-progress]");
        if (progress && progress.textContent !== result.progress) progress.textContent = result.progress;
        if (!["queued", "running"].includes(result.status)) {
          const resultRegion = operation.closest("#ranking-result");
          if (resultRegion) {
            const documentFragment = new DOMParser().parseFromString(result.html, "text/html");
            resultRegion.replaceChildren(...documentFragment.body.childNodes);
            syncScorePreview();
            syncRuleSaveButtons();
          }
          document.querySelectorAll("[data-operation-busy]").forEach((button) => {
            button.removeAttribute("data-operation-busy");
            const form = document.getElementById(button.dataset.requiresSavedForm || "");
            button.disabled = Boolean(form && form.dataset.dirty === "true");
          });
          return;
        }
      } catch (error) {
        failures += 1;
        if (failures >= 3) {
          const progress = operation.querySelector("[data-task-progress]");
          if (progress) progress.textContent = operation.dataset.pollError || "";
          return;
        }
      }
      window.setTimeout(poll, 2000);
    };
    window.addEventListener("pagehide", () => { stopped = true; }, { once: true });
    window.setTimeout(poll, 1500);
  });

  document.querySelectorAll("[data-collection-watch]").forEach((workspace) => {
    let stopped = false;
    const poll = async () => {
      if (stopped || !workspace.isConnected) return;
      try {
        const response = await fetch(workspace.dataset.collectionWatch, {
          credentials: "same-origin", headers: { Accept: "application/json" },
        });
        if (!response.ok) return;
        const result = await response.json();
        const progress = workspace.querySelector("[data-collection-progress]");
        if (progress) progress.textContent = result.progress;
        if (!["queued", "running"].includes(result.state)) {
          workspace.querySelectorAll("[data-fetch-button]").forEach((button) => {
            button.disabled = false;
            button.textContent = button.dataset.readyLabel;
          });
          const dirty = [...workspace.querySelectorAll("form[data-dirty-track]")].some(form => (
            form.dataset.dirty === "true" && !form.closest("[hidden]")
          ));
          if (!dirty) {
            const address = new URL(window.location.href);
            address.searchParams.delete("activated");
            address.searchParams.delete("fetch_requested");
            window.location.replace(address);
          }
          return;
        }
      } catch (error) {
        return;
      }
      window.setTimeout(poll, document.hidden ? 10000 : 2000);
    };
    window.addEventListener("pagehide", () => { stopped = true; }, { once: true });
    window.setTimeout(poll, 1500);
  });

  document.querySelectorAll("[data-email-schedule-preview]").forEach((builder) => {
    const output = builder.querySelector("[data-schedule-preview-result]");
    if (!output) return;
    let timer;
    let revision = 0;
    const refresh = async () => {
      if (builder.closest("[hidden]")) return;
      const ownRevision = ++revision;
      const scope = builder.closest("form") || builder;
      const mode = builder.dataset.fixedMode
        || scope.querySelector("[name='schedule_mode']:checked")?.value || "interval";
      const params = new URLSearchParams({
        schedule_mode: mode,
        send_interval_hours: scope.querySelector("[name='send_interval_hours']")?.value || "24",
        schedule_time: scope.querySelector("[name='schedule_time']")?.value ?? "09:00",
        schedule_timezone: scope.querySelector("[name='schedule_timezone']")?.value ?? "Pacific/Auckland",
        schedule_weekdays: scope.querySelector("[name='schedule_weekdays']")
          ? [...scope.querySelectorAll("[name='schedule_weekdays']:checked")].map(e => e.value).join(",")
          : "0,1,2,3,4,5,6",
      });
      if (builder.dataset.groupId) params.set("group_id", builder.dataset.groupId);
      try {
        const response = await fetch(`/admin/email-schedule-preview?${params}`, {
          credentials: "same-origin", headers: { Accept: "application/json" },
        });
        const result = await response.json();
        if (ownRevision === revision) {
          output.textContent = result.label || result.error || "";
          output.classList.toggle("field-error", !response.ok);
          scope.querySelectorAll("[data-email-schedule-summary]").forEach(summary => {
            summary.textContent = output.textContent;
            summary.hidden = !output.textContent;
            summary.classList.toggle("field-error", !response.ok);
          });
        }
      } catch (error) {
        if (ownRevision === revision) output.textContent = "";
      }
    };
    const changed = () => {
      window.clearTimeout(timer);
      timer = window.setTimeout(refresh, 250);
    };
    (builder.closest("form") || builder).addEventListener("input", changed);
    (builder.closest("form") || builder).addEventListener("change", changed);
    refresh();
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

  // A model-list refresh runs in the Research worker, not in this web process. While one is
  // queued or running, ask for its phase and reload once it changes, so the new list, or the
  // reason it failed, shows without a manual reload.
  const refreshStatus = document.querySelector("[data-refresh-status-url]");
  if (refreshStatus instanceof HTMLElement) {
    const statusUrl = refreshStatus.dataset.refreshStatusUrl || "";
    const shownPhase = refreshStatus.dataset.refreshPhase || "";
    const doneUrl = new URL(
      refreshStatus.dataset.refreshDoneUrl || window.location.href,
      window.location.href,
    );
    const startedAt = Date.now();
    const showResult = () => {
      const here = new URL(window.location.href);
      const target = new URL(doneUrl.href);
      here.hash = "";
      target.hash = "";
      // Navigating to the page already shown, give or take a #fragment, may only scroll.
      if (here.href === target.href) {
        window.location.reload();
      } else {
        window.location.replace(doneUrl.href);
      }
    };
    const checkRefresh = async () => {
      try {
        const response = await fetch(statusUrl, {
          cache: "no-store",
          credentials: "same-origin",
          headers: { Accept: "application/json" },
        });
        if (response.redirected) {
          return; // Signed out. The next reload goes to the login page.
        }
        if (response.ok) {
          const { phase } = await response.json();
          if (phase && phase !== shownPhase) {
            showResult();
            return;
          }
        }
      } catch (error) {
        console.warn("Could not check the model list refresh", error);
      }
      // A stuck refresh turns into "failed" within about four minutes (one queued, three
      // running), so keep asking well past that: every 2 seconds at first, then every 10.
      const elapsed = Date.now() - startedAt;
      if (elapsed < 15 * 60 * 1000) {
        window.setTimeout(checkRefresh, elapsed < 2 * 60 * 1000 ? 2000 : 10000);
      }
    };
    window.setTimeout(checkRefresh, 2000);
  }

  // Research conclusion: on a wide screen a citation number shows its source in the side column
  // instead of opening a new tab. When the side column sits under the text (narrow screens), or
  // the cited item has no card, the link keeps its normal behaviour.
  const citePanel = document.querySelector("[data-cite-panel]");
  const citeSide = citePanel?.closest(".nb-side");
  if (citePanel instanceof HTMLElement && citeSide instanceof HTMLElement) {
    const citeEmpty = citePanel.querySelector("[data-cite-empty]");
    document.addEventListener("click", (event) => {
      const link = event.target instanceof Element ? event.target.closest("a.cite[data-cite]") : null;
      if (
        !(link instanceof HTMLAnchorElement) || event.defaultPrevented || event.button !== 0
        || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey
        || getComputedStyle(citeSide).position !== "sticky"
      ) {
        return;
      }
      const id = CSS.escape(link.dataset.cite || "");
      const card = citePanel.querySelector(`[data-cite-card="${id}"]`);
      if (!(card instanceof HTMLElement)) {
        return;
      }
      event.preventDefault();
      citePanel.querySelectorAll("[data-cite-card]").forEach((other) => {
        other.hidden = other !== card;
      });
      if (citeEmpty instanceof HTMLElement) {
        citeEmpty.hidden = true;
      }
      document.querySelectorAll("a.cite[aria-current]").forEach((other) => {
        other.removeAttribute("aria-current");
      });
      document.querySelectorAll(`a.cite[data-cite="${id}"]`).forEach((same) => {
        same.setAttribute("aria-current", "true");
      });
    });
  }

  // Bulk selection whose row checkboxes sit in a table outside the form (form="…"): count the
  // picked rows, keep the header checkbox in step, and hold the submit until something is picked.
  // Delegated by form id, because the list can be re-rendered in place.
  const bulkBoxes = (form) => [...form.elements].filter(
    (element) => element instanceof HTMLInputElement && element.type === "checkbox",
  );
  const syncBulk = (form) => {
    const id = CSS.escape(form.id);
    const all = bulkBoxes(form);
    const picked = all.filter((box) => box.checked).length;
    const countLabel = document.querySelector(`[data-bulk-count="${id}"]`);
    if (countLabel instanceof HTMLElement) {
      countLabel.textContent = (countLabel.dataset.template || "__COUNT__")
        .replace("__COUNT__", String(picked));
    }
    document.querySelectorAll(`[data-bulk-submit="${id}"]`).forEach((button) => {
      button.disabled = picked === 0;
    });
    // Wide screens use the table-head checkbox, phones the one beside the count.
    document.querySelectorAll(`[data-bulk-all="${id}"]`).forEach((selectAll) => {
      if (selectAll instanceof HTMLInputElement) {
        selectAll.checked = all.length > 0 && picked === all.length;
        selectAll.indeterminate = picked > 0 && picked < all.length;
      }
    });
  };
  const syncAllBulk = () => {
    document.querySelectorAll("form[data-bulk-form][id]").forEach(syncBulk);
  };
  document.addEventListener("change", (event) => {
    const input = event.target;
    if (!(input instanceof HTMLInputElement)) {
      return;
    }
    if (input.dataset.bulkAll) {
      const form = document.getElementById(input.dataset.bulkAll);
      if (form instanceof HTMLFormElement) {
        bulkBoxes(form).forEach((box) => {
          box.checked = input.checked;
        });
        syncBulk(form);
      }
    } else if (input.form?.matches("[data-bulk-form][id]")) {
      syncBulk(input.form);
    }
  });
  document.addEventListener("htmx:afterSettle", syncAllBulk);
  syncAllBulk();

  // Lanes (see "Lanes" in admin.css): each fits the window below where it starts, leaving room
  // for the pager under a split list, so reading one lane to its end never moves the page. A
  // lane that starts further down fits the window once scrolled to. A lane is a scroller only
  // on a wide sheet; anywhere else it keeps no height of its own.
  const LANE = ".tbl-lanes > tbody[data-lane], .cols.lanes > *";
  const fitLanes = () => {
    const viewport = window.innerHeight;
    document.querySelectorAll(LANE).forEach((lane) => {
      if (getComputedStyle(lane).overflowY !== "auto") {
        lane.style.removeProperty("--lane-h");
        return;
      }
      const top = lane.getBoundingClientRect().top + window.scrollY;
      const section = lane.closest("section") || lane;
      const sectionTop = section.getBoundingClientRect().top + window.scrollY;
      const after = lane.closest(".tbl-wrap")?.nextElementSibling;
      const reserve = 24 + (after?.matches(".pager") ? after.offsetHeight + 10 : 0);
      const start = sectionTop < viewport * 0.6 ? top : 16 + (top - sectionTop);
      const height = Math.max(viewport * 0.5, viewport - start - reserve);
      lane.style.setProperty("--lane-h", `${Math.round(height)}px`);
    });
  };
  let fitQueued = false;
  const queueFit = () => {
    if (!fitQueued) {
      fitQueued = true;
      requestAnimationFrame(() => {
        fitQueued = false;
        fitLanes();
      });
    }
  };
  fitLanes();
  window.addEventListener("resize", queueFit);
  document.addEventListener("htmx:afterSettle", queueFit);
  document.addEventListener("toggle", queueFit, true);
  document.fonts?.ready.then(queueFit);

  // A swap that re-renders a block of lanes (a read toggle re-renders its section) keeps each
  // lane where it was scrolled to, rather than jumping back to its top.
  let laneScroll = null;
  const lanesIn = (region) => [
    ...(region.matches(LANE) ? [region] : []),
    ...region.querySelectorAll(LANE),
  ];
  document.addEventListener("htmx:beforeSwap", (event) => {
    const target = event.detail.target;
    laneScroll = target instanceof Element && target.id
      ? { region: target.id, tops: lanesIn(target).map((lane) => lane.scrollTop) }
      : null;
  });
  // Capture runs this before the refocus above, so focus lands in a lane already in place.
  document.addEventListener("htmx:afterSwap", () => {
    const region = laneScroll && document.getElementById(laneScroll.region);
    if (region) {
      fitLanes();
      lanesIn(region).forEach((lane, index) => {
        lane.scrollTop = laneScroll.tops[index] ?? 0;
      });
    }
    laneScroll = null;
  }, true);

  // Paging a section returns to it. A pager link remembers its section for the next page, which
  // brings that section into view only when it starts in the lower half of the window. On a
  // wide sheet, or on a short page, the section is already in view, so the page stays put.
  const REVEAL_KEY = "beehive:reveal-section";
  document.addEventListener("click", (event) => {
    const plain = event.button === 0 && !event.metaKey && !event.ctrlKey && !event.shiftKey && !event.altKey;
    const link = plain && event.target instanceof Element ? event.target.closest(".pager a[href]") : null;
    const section = link?.closest("section[id]");
    if (!section) {
      return;
    }
    try {
      window.sessionStorage.setItem(REVEAL_KEY, JSON.stringify({
        path: new URL(link.href, window.location.href).pathname,
        id: section.id,
        at: Date.now(),
      }));
    } catch (error) {
      console.warn("Could not remember the paged section", error);
    }
  });
  try {
    const pending = JSON.parse(window.sessionStorage.getItem(REVEAL_KEY) || "null");
    window.sessionStorage.removeItem(REVEAL_KEY);
    const arrival = performance.getEntriesByType?.("navigation")?.[0]?.type;
    const fresh = pending
      && pending.path === window.location.pathname
      && Date.now() - pending.at < 20000
      && arrival !== "back_forward"
      && arrival !== "reload"
      && !window.location.hash;
    const section = fresh ? document.getElementById(pending.id) : null;
    if (section && section.getBoundingClientRect().top > window.innerHeight / 2) {
      section.scrollIntoView({ block: "start" });
    }
  } catch (error) {
    console.warn("Could not return to the paged section", error);
  }

  // A photo its CDN will not serve becomes the empty frame shown for a listing without one,
  // rather than the browser's broken-image mark.
  const PHOTO = "img.lot-img, img.plate-img";
  const toFrame = (img) => {
    const frame = document.createElement("span");
    frame.className = img.className;
    frame.setAttribute("aria-hidden", "true");
    img.replaceWith(frame);
  };
  document.addEventListener("error", (event) => {
    if (event.target instanceof HTMLImageElement && event.target.matches(PHOTO)) {
      toFrame(event.target);
    }
  }, true);
  document.querySelectorAll(PHOTO).forEach((img) => {
    if (img.complete && img.naturalWidth === 0 && img.getAttribute("src")) {
      toFrame(img);
    }
  });

  // Opening a story reads it: /items/{id}/open marks it read for the Owner on the way to the
  // article, so its row takes the read state here in place while the article opens in another
  // tab. The row stays put, and its read toggle now offers to mark it unread.
  const readInPlace = (link) => {
    const row = link.closest("tr.kb-row");
    const toggle = row?.querySelector("button.rd:not(.is-read)");
    if (!(toggle instanceof HTMLButtonElement)) {
      return;
    }
    row.classList.add("is-read");
    toggle.classList.add("is-read");
    const state = toggle.form?.elements.namedItem("is_read");
    if (state instanceof HTMLInputElement) {
      state.value = "0";
    }
    if (toggle.dataset.readTitle) {
      toggle.title = toggle.dataset.readTitle;
    }
    if (toggle.dataset.readAria) {
      toggle.setAttribute("aria-label", toggle.dataset.readAria);
    }
    if (toggle.dataset.readFeedback) {
      toggle.dataset.feedbackMessage = toggle.dataset.readFeedback;
    }
  };
  const openedLink = (event) => (
    event.target instanceof Element ? event.target.closest("a[data-kb-open]") : null
  );
  document.addEventListener("click", (event) => {
    const link = openedLink(event);
    if (link) {
      readInPlace(link);
    }
  });
  document.addEventListener("auxclick", (event) => {
    const link = event.button === 1 ? openedLink(event) : null;
    if (link) {
      readInPlace(link);
    }
  });

  // Keyboard reading on the reading pages: / or f focuses search, j and k select a row (a
  // story, listing or lot, or a gallery plate), o or Enter opens it. The Owner's keys press the
  // selected row's own controls: m marks it read or unread, + and - rate it relevant or not, w
  // watches a lot. Rows and the search field are looked up on every key press, since an htmx
  // swap can replace them.
  const findSearch = () => document.querySelector("[data-kb-search]");
  const selectionStatus = document.getElementById("kb-status");
  let selectedRow = null;

  // Only rows on screen take part: a closed history disclosure keeps its rows out of reach.
  const isShown = (row) => (
    row.isConnected && !row.closest("details:not([open])") && row.getClientRects().length > 0
  );

  // The row keyboard reading moves from: the selected one, or, once a swap has re-rendered it,
  // the row that now holds focus in the same slot.
  const currentRow = () => {
    if (selectedRow && isShown(selectedRow)) {
      return selectedRow;
    }
    const focused = document.activeElement?.closest?.(".kb-row");
    return focused instanceof HTMLElement && isShown(focused) ? focused : null;
  };

  if (!(findSearch() instanceof HTMLInputElement)) {
    return;
  }

  const isTyping = (target) => (
    target instanceof HTMLElement
    && (
      target.isContentEditable
      || ["INPUT", "SELECT", "TEXTAREA"].includes(target.tagName)
    )
  );

  const kbRows = () => [...document.querySelectorAll(".kb-row")].filter(isShown);

  const selectRow = (row) => {
    kbRows().forEach((other) => {
      other.classList.toggle("is-selected", other === row);
      other.tabIndex = other === row ? 0 : -1;
    });
    selectedRow = row;
    row.focus({ preventScroll: true });
    row.scrollIntoView({ block: "nearest" });
    if (selectionStatus) {
      const replacements = {
        __CHANNEL__: row.dataset.channel || "",
        __SCORE__: row.querySelector(".score")?.textContent.trim() || "",
        __SUMMARY__: row.querySelector("[data-kb-open]")?.firstChild?.textContent.trim() || "",
      };
      const message = (selectionStatus.dataset.selectionTemplate || "").replace(
        /__(CHANNEL|SCORE|SUMMARY)__/g,
        (token) => replacements[token] ?? token,
      );
      selectionStatus.textContent = "";
      requestAnimationFrame(() => {
        selectionStatus.textContent = message;
      });
    }
  };

  // A swap re-renders the row a key acted on; the one that took its place keeps the selection.
  document.addEventListener("htmx:afterSettle", () => {
    if (selectedRow && !selectedRow.isConnected) {
      const row = document.activeElement?.closest?.(".kb-row");
      if (row instanceof HTMLElement) {
        row.classList.add("is-selected");
        selectedRow = row;
      }
    }
  });

  const ROW_KEYS = {
    m: "button.rd",
    "+": ".vote button[value='1']",
    "=": ".vote button[value='1']",
    "-": ".vote button[value='-1']",
    w: "button.tg-watch",
  };

  document.addEventListener("keydown", (event) => {
    if (event.defaultPrevented || event.altKey || event.ctrlKey || event.metaKey) {
      return;
    }

    const key = event.key.toLowerCase();
    const search = findSearch();
    if (!isTyping(event.target) && (key === "/" || key === "f") && search instanceof HTMLInputElement) {
      event.preventDefault();
      search.focus();
      search.select();
      return;
    }

    if (isTyping(event.target)) {
      return;
    }

    if (key === "j" || key === "k") {
      const rows = kbRows();
      if (rows.length === 0) {
        return;
      }
      event.preventDefault();
      const current = rows.indexOf(currentRow());
      const next = current < 0
        ? (key === "j" ? 0 : rows.length - 1)
        : Math.max(0, Math.min(current + (key === "j" ? 1 : -1), rows.length - 1));
      selectRow(rows[next]);
      return;
    }

    const row = currentRow();
    const control = row && ROW_KEYS[key] ? row.querySelector(ROW_KEYS[key]) : null;
    if (control instanceof HTMLButtonElement) {
      event.preventDefault();
      control.click();
      return;
    }

    if ((key === "o" && row) || (key === "enter" && row && event.target === row)) {
      const link = row.querySelector("[data-kb-open]");
      if (link instanceof HTMLAnchorElement) {
        event.preventDefault();
        readInPlace(link);
        window.open(link.href, "_blank", "noopener,noreferrer");
      }
    }
  });
})();
