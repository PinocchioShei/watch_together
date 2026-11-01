// One import workflow for user and administrator pages; only endpoint/auth differ.
export function createImportForm({ form, api, endpoint, targetsPath, upload,
  estimateSeconds, onStart, onProcessing, onSuccess, onError }) {
  const field = (name) => form.querySelector(`[name="${name}"]`);
  const files = field("file");
  const mode = field("import_mode");
  const target = field("target_work_key");
  const type = field("media_type");
  const cover = field("cover");
  const workName = field("work_name");
  const submit = form.querySelector("button[type='submit']");
  let targets = [];
  let maxFiles = 30;
  let busy = false;

  function update() {
    const append = mode.value === "append";
    target.closest(".import-target-row").classList.toggle("hidden", !append);
    const needsName = !append && files.files?.length > 1;
    workName.closest(".import-name-row").classList.toggle("hidden", !needsName);
    target.disabled = !append;
    target.required = append;
    workName.disabled = !needsName;
    workName.required = needsName;
    cover.disabled = append;
    type.disabled = append;
    type.required = !append;
    if (append) {
      cover.value = "";
      type.value = targets.find((item) => item.workKey === target.value)?.type || "movie";
    }
    form.querySelector(".import-hint").textContent = append
        ? `Append up to ${maxFiles} files of the same kind as the selected collection. Video and audio cannot be mixed.`
      : `Select 1–${maxFiles} videos or audio files. A single work uses the filename; multiple files require a collection name.`;
  }

  async function refreshTargets() {
    const data = await api(targetsPath, { method: "GET" });
    targets = data.items || [];
    maxFiles = data.maxFiles || 30;
    const previous = target.value;
    target.replaceChildren();
    const placeholder = document.createElement("option");
    placeholder.value = "";
    placeholder.textContent = "Select a video or audio collection";
    target.appendChild(placeholder);
    [...targets].sort((a, b) => String(a.name).localeCompare(String(b.name))).forEach((item) => {
      const option = document.createElement("option");
      option.value = item.workKey;
      const label = item.mediaKind === "video" ? "Video" : "Audio";
      option.textContent = `[${label}] ${item.name} (${item.trackCount} items)`;
      target.appendChild(option);
    });
    target.value = targets.some((item) => item.workKey === previous) ? previous : (targets[0]?.workKey || "");
    update();
  }

  mode.addEventListener("change", async () => {
    update();
    if (mode.value === "append") {
      try { await refreshTargets(); } catch (error) { onError(error); }
    }
  });
  target.addEventListener("change", update);
  files.addEventListener("change", update);
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (busy) return;
    const selected = Array.from(files.files || []);
    const append = mode.value === "append";
    try {
      if (!selected.length || selected.length > maxFiles) throw new Error(`Choose 1 to ${maxFiles} media files.`);
      if (append && !target.value) throw new Error("Choose an existing video or audio collection first.");
      if (!append && !type.value) throw new Error("Choose media type first.");
      if (!append && selected.length > 1 && !workName.value.trim()) throw new Error("A collection name is required for multiple files.");
      const data = new FormData();
      selected.forEach((file) => data.append("file", file));
      data.append("import_mode", mode.value);
      data.append("media_type", type.value || "movie");
      if (append) data.append("target_work_key", target.value);
      else {
        if (selected.length > 1) data.append("work_name", workName.value.trim());
        if (cover.files?.[0]) data.append("cover", cover.files[0]);
      }
      busy = true;
      submit.disabled = true;
      const estimate = selected.reduce((total, file) => total + estimateSeconds(file), 0);
      onStart();
      const result = await upload(endpoint, data, () => onProcessing(estimate));
      if (!result.ok) throw new Error(result.errors?.map((item) => item.detail).join("; ") || "Import failed.");
      files.value = "";
      cover.value = "";
      workName.value = "";
      await onSuccess(result);
      await refreshTargets();
    } catch (error) {
      onError(error);
    } finally {
      busy = false;
      submit.disabled = false;
    }
  });
  update();
  return { refreshTargets };
}

export function importResultMessage(result) {
  const kind = result.mediaKind === "video" ? "video" : "audio";
  return `${result.appendMode ? "Appended" : "Imported"} ${result.imported} ${kind} item(s) ${result.appendMode ? "to" : "into"} ${result.workKey}.`;
}
