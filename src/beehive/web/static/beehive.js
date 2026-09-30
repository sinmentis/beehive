(() => {
  let focusKey = "";
  let feedbackMessage = "";
  let fallbackFocusSelector = "";
  let refocusSlot = null;

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
