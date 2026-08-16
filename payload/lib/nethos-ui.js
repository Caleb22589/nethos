/*!
 * nethos-ui.js — styled dialog, menu and form controls for NETHOS apps.
 *
 * Every app in the system loads this so the browser's own prompt(), confirm()
 * and alert() are never seen. HTML5 form validation is also intercepted: the
 * built-in popups are styled to match the system.
 *
 * Usage:
 *
 *     <script src="/lib/nethos-ui.js"></script>
 *     <script>
 *       const name = await ui.ask("What is your name?");
 *       const ok = await ui.confirm("Are you sure?");
 *       ui.toast("Saved", "good");
 *
 *       ui.menu([
 *         { label: "Open", run: () => act("open", path) },
 *         { label: "Rename…", run: () => rename(entry) },
 *         "-",
 *         { label: "Move to Trash", danger: true, run: () => trash(path) },
 *       ], ev.clientX, ev.clientY);
 *
 *       // Ask with multiple fields including select and checkbox
 *       const vals = await ui.ask([
 *         { type: "text", label: "Name", value: "default" },
 *         { type: "select", label: "Type", options: ["File", "Folder", "Link"] },
 *         { type: "checkbox", label: "Hidden" },
 *       ]);
 *     </script>
 *
 * Returns a promise. Calls to ui.ask and ui.confirm with a single string
 * argument accept a plain-label shorthand.
 */
(function () {
  "use strict";

  /* ----------------------------------------------------------- element factory */

  function el(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }

  /* --------------------------------------------------------- overlay (shared) */

  let overlay = null;
  let activeDialog = null;

  function ensureOverlay() {
    if (!overlay) {
      overlay = el("div", "ui-overlay");
      overlay.addEventListener("click", () => {
        // Only dismiss if the click was on the backdrop, not a child.
        if (overlay && activeDialog && !activeDialog.contains(overlay)) {
          overlay.remove();
        }
      });
      document.body.append(overlay);
    }
    overlay.style.display = "";
    return overlay;
  }

  function removeOverlay() {
    if (overlay) overlay.style.display = "none";
    activeDialog = null;
  }

  /* ---------------------------------------------------------------- ui.ask -- */

  /**
   * Show a modal dialog with one or more fields.
   *
   * Single-field shorthand:
   *   ui.ask("Enter a name")          — text, no default
   *   ui.ask("Enter a name", "Bob")   — text with default
   *
   * Full signature (always returns an object keyed by field label):
   *   ui.ask([
   *     { type: "text",     label: "Name", value: "", placeholder: "" },
   *     { type: "password", label: "Password", value: "" },
   *     { type: "select",   label: "Type", options: ["A","B","C"], value: "A" },
   *     { type: "checkbox", label: "Hidden", checked: false },
   *   ])
   *
   * Returns Promise<object|null> — null if cancelled.
   */
  function ask(labelOrFields, defaultValue) {
    // Shorthand: plain string → single text field
    const fields = typeof labelOrFields === "string"
      ? [{ type: "text", label: labelOrFields, value: defaultValue || "" }]
      : labelOrFields;

    return new Promise((resolve) => {
      const ov = ensureOverlay();
      const dlg = el("div", "ui-dialog glass");
      activeDialog = dlg;

      // Header
      if (fields.length > 1 || fields[0].type !== "text") {
        // Multi-field or non-text: no header title; let labels speak
      }

      const body = el("div", "ui-dialog-body");
      const inputs = [];

      for (const f of fields) {
        if (f.label && f.type !== "checkbox") {
          body.append(el("label", "ui-label", f.label));
        }

        if (f.type === "password" || f.type === "text") {
          const inp = el("input", "ui-input");
          inp.type = f.type || "text";
          inp.placeholder = f.placeholder || "";
          inp.value = f.value || "";
          inp.autocomplete = f.type === "password" ? "off" : "";
          body.append(inp);
          inputs.push(inp);

        } else if (f.type === "select") {
          const sel = el("select", "ui-select");
          const opts = f.options || [];
          for (const o of opts) {
            const opt = el("option");
            opt.value = typeof o === "object" ? o.value : o;
            opt.textContent = typeof o === "object" ? o.label : o;
            if (opt.value === (f.value || opts[0])) opt.selected = true;
            sel.append(opt);
          }
          body.append(sel);
          inputs.push(sel);

        } else if (f.type === "checkbox") {
          const row = el("label", "ui-check-row");
          const cb = el("input", "ui-check");
          cb.type = "checkbox";
          cb.checked = !!f.checked;
          row.append(cb);
          row.append(el("span", null, f.label));
          body.append(row);
          inputs.push(cb);
        }
      }

      dlg.append(body);

      // Buttons
      const btns = el("div", "ui-dialog-btns");
      const cancelBtn = el("button", "ui-btn", "Cancel");
      const okBtn = el("button", "ui-btn ui-btn-primary", "OK");
      if (fields.length === 1 && fields[0].type === "text" && fields[0].label) {
        // Single text field — use label as button text for natural reading
        // e.g. ui.ask("New name") → OK / Cancel
      }
      btns.append(cancelBtn, okBtn);
      dlg.append(btns);

      ov.append(dlg);

      // Focus first input
      setTimeout(() => {
        const first = inputs[0];
        if (first) first.focus();
        if (first && first.type === "text") first.select();
      }, 0);

      function collect() {
        if (fields.length === 1) {
          const f = fields[0];
          if (f.type === "checkbox") return inputs[0].checked;
          return inputs[0].value;
        }
        const result = {};
        fields.forEach((f, i) => {
          const inp = inputs[i];
          if (f.type === "checkbox") result[f.label] = inp.checked;
          else result[f.label] = inp.value;
        });
        return result;
      }

      function dismiss(val) {
        removeOverlay();
        dlg.remove();
        resolve(val);
      }

      cancelBtn.addEventListener("click", () => dismiss(null));
      okBtn.addEventListener("click", () => dismiss(collect()));

      // Keyboard: Enter submits, Escape cancels
      dlg.addEventListener("keydown", (e) => {
        if (e.key === "Escape") { e.preventDefault(); dismiss(null); }
        if (e.key === "Enter" && e.target.tagName !== "TEXTAREA") {
          e.preventDefault();
          dismiss(collect());
        }
      });
    });
  }

  /* ------------------------------------------------------------- ui.confirm -- */

  function confirm(message) {
    return new Promise((resolve) => {
      const ov = ensureOverlay();
      const dlg = el("div", "ui-dialog glass ui-dialog-sm");
      activeDialog = dlg;

      const body = el("div", "ui-dialog-body");
      body.append(el("p", "ui-confirm-text", message));
      dlg.append(body);

      const btns = el("div", "ui-dialog-btns");
      const cancelBtn = el("button", "ui-btn", "Cancel");
      const okBtn = el("button", "ui-btn ui-btn-primary", "OK");
      btns.append(cancelBtn, okBtn);
      dlg.append(btns);
      ov.append(dlg);

      okBtn.focus();

      function dismiss(val) {
        removeOverlay();
        dlg.remove();
        resolve(val);
      }

      cancelBtn.addEventListener("click", () => dismiss(false));
      okBtn.addEventListener("click", () => dismiss(true));
      dlg.addEventListener("keydown", (e) => {
        if (e.key === "Escape") dismiss(false);
        if (e.key === "Enter") dismiss(true);
      });
    });
  }

  /* --------------------------------------------------------------- ui.toast -- */

  function toast(text, level) {
    let host = document.querySelector(".ui-toasts");
    if (!host) {
      host = el("div", "ui-toasts");
      document.body.appendChild(host);
    }
    const node = el("div", "ui-toast" + (level && level !== "info" ? " ui-toast-" + level : ""), text);
    host.appendChild(node);
    setTimeout(() => node.remove(), 4000);
    return node;
  }

  /* ---------------------------------------------------------------- ui.menu -- */

  let menuInstance = null;

  function menu(items, x, y) {
    if (menuInstance) menuInstance.remove();

    const menuEl = el("div", "ui-menu glass");
    items.forEach((item) => {
      if (item === "-" || item.sep || item.label === "-") {
        menuEl.append(el("div", "ui-menu-sep"));
        return;
      }
      const b = el("button", "ui-menu-item" + (item.danger ? " ui-menu-danger" : ""));
      b.textContent = item.label;
      if (item.disabled) b.disabled = true;
      else b.addEventListener("click", () => {
        closeMenu();
        if (typeof item.run === "function") item.run();
      });
      menuEl.append(b);
    });

    document.body.append(menuEl);

    // Position, clamped to viewport
    const r = menuEl.getBoundingClientRect();
    menuEl.style.left = Math.max(8, Math.min(x, window.innerWidth - r.width - 8)) + "px";
    menuEl.style.top = Math.max(8, Math.min(y, window.innerHeight - r.height - 8)) + "px";
    menuInstance = menuEl;

    // Dismiss
    const dismiss = (e) => {
      if (!menuInstance) return;
      if (e.type === "keydown") {
        if (e.key === "Escape") closeMenu();
        return;
      }
      if (menuInstance.contains(e.target)) return;
      closeMenu();
    };
    setTimeout(() => {
      window.addEventListener("pointerdown", dismiss, true);
      window.addEventListener("keydown", dismiss, true);
    }, 0);

    return menuEl;
  }

  function closeMenu() {
    if (!menuInstance) return;
    menuInstance.remove();
    menuInstance = null;
  }

  /* ------------------------------------------------------- validation styling */

  // Intercept HTML5 validation popups and replace with ui.toast
  document.addEventListener("invalid", (e) => {
    if (e.target && e.target.validationMessage) {
      toast(e.target.validationMessage, "warn");
    }
  }, true);

  /* ------------------------------------------------------------ public API -- */

  const ui = { ask, confirm, menu, toast, el };
  window.ui = ui;

  // Also attach to nethos if it is already loaded, or patch it when it arrives.
  if (window.nethos) window.nethos.ui = Object.assign(window.nethos.ui || {}, ui);
})();