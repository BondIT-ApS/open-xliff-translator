/*
 * Open XLIFF Translator - UI behaviour.
 *
 * Served from /static so the Content-Security-Policy can keep script-src 'self'
 * with no 'unsafe-inline'. Do not move this back into an inline <script> block
 * and do not add onclick="..." attributes to templates/index.html - the browser
 * will refuse to run them. Wire new controls up with addEventListener here.
 *
 * Loaded with defer, so the DOM is parsed by the time this runs.
 */

let currentJobId = null;
let pollInterval = null;

function previewFile() {
    const fileInput = document.getElementById("fileInput");
    const fileContent = document.getElementById("fileContent");

    if (!fileInput.files.length) {
        fileContent.textContent = "No file uploaded yet.";
        return;
    }

    const file = fileInput.files[0];
    const reader = new FileReader();

    reader.onload = function (e) {
        const xmlString = e.target.result;
        try {
            const parser = new DOMParser();
            const xmlDoc = parser.parseFromString(xmlString, "text/xml");

            let previewText = "";
            const sources = xmlDoc.getElementsByTagName("source");
            for (let i = 0; i < sources.length && i < 20; i++) {
                previewText += `🔹 ${sources[i].textContent}\n`;
            }

            fileContent.textContent = previewText || "No translatable content found.";
        } catch (error) {
            fileContent.textContent = "Error parsing XLIFF file.";
        }
    };

    reader.readAsText(file);
}

function uploadFile() {
    const fileInput = document.getElementById("fileInput");
    const status = document.getElementById("status");
    const downloadLink = document.getElementById("downloadLink");
    const progressSection = document.getElementById("progressSection");
    const progressBar = document.getElementById("progressBar");
    const progressText = document.getElementById("progressText");
    const cancelBtn = document.getElementById("cancelBtn");

    if (!fileInput.files.length) {
        alert("Please select a file first.");
        return;
    }

    const file = fileInput.files[0];
    const formData = new FormData();
    formData.append("file", file);

    status.textContent = "Uploading...";
    downloadLink.style.display = "none";
    progressSection.style.display = "block";
    progressBar.style.width = "0%";
    progressText.textContent = "Starting translation...";
    cancelBtn.style.display = "inline-block";

    fetch("/upload", {
        method: "POST",
        body: formData,
    })
    .then(response => response.json())
    .then(data => {
        if (data.job_id) {
            currentJobId = data.job_id;
            status.textContent = "Translation in progress...";
            pollInterval = setInterval(() => pollProgress(currentJobId), 1000);
        } else {
            status.textContent = "Error: " + (data.detail || "Unknown error");
            progressSection.style.display = "none";
        }
    })
    .catch(error => {
        status.textContent = "Failed to connect to the server.";
        progressSection.style.display = "none";
        console.error("Error:", error);
    });
}

function pollProgress(jobId) {
    fetch(`/progress/${jobId}`)
    .then(response => response.json())
    .then(data => {
        const progressBar = document.getElementById("progressBar");
        const progressText = document.getElementById("progressText");
        const status = document.getElementById("status");
        const downloadLink = document.getElementById("downloadLink");
        const progressSection = document.getElementById("progressSection");
        const cancelBtn = document.getElementById("cancelBtn");

        if (data.total > 0) {
            const pct = Math.round((data.completed / data.total) * 100);
            progressBar.style.width = pct + "%";
            progressText.textContent = `${data.completed} / ${data.total} segments translated (${pct}%)`;
        }

        if (data.status === "completed") {
            clearInterval(pollInterval);
            pollInterval = null;
            progressBar.style.width = "100%";
            progressText.textContent = `${data.total} / ${data.total} segments translated (100%)`;
            status.textContent = "Translation complete!";
            if (data.terms_applied > 0) {
                status.textContent += ` (${data.terms_applied} vocabulary overrides applied)`;
            }
            cancelBtn.style.display = "none";
            if (data.download_url) {
                downloadLink.href = data.download_url;
                downloadLink.textContent = "Download Translated File";
                downloadLink.style.display = "inline-block";
            }
        } else if (data.status === "failed") {
            clearInterval(pollInterval);
            pollInterval = null;
            status.textContent = "Translation failed: " + (data.error || "Unknown error");
            progressSection.style.display = "none";
        } else if (data.status === "cancelled") {
            clearInterval(pollInterval);
            pollInterval = null;
            status.textContent = "Translation was cancelled.";
            progressSection.style.display = "none";
        }
    })
    .catch(error => {
        console.error("Progress poll error:", error);
    });
}

function cancelTranslation() {
    if (!currentJobId) return;

    fetch(`/progress/${currentJobId}`, { method: "DELETE" })
    .then(response => response.json())
    .then(() => {
        document.getElementById("status").textContent = "Cancellation requested...";
    })
    .catch(error => {
        console.error("Cancel error:", error);
    });
}

document.getElementById("currentYear").textContent = new Date().getFullYear();
document.getElementById("fileInput").addEventListener("change", previewFile);
document.getElementById("uploadBtn").addEventListener("click", uploadFile);
document.getElementById("cancelBtn").addEventListener("click", cancelTranslation);

// --- Vocabulary overrides (issue #142) -------------------------------------
// Was an inline <script> in the template, driven by onclick="addTerm()" and
// onchange="importTerms()". Both forms are blocked by script-src 'self'.

const VOCAB_LANG = "da";

function showVocabError(message) {
    document.getElementById("vocabError").textContent = message || "";
}

async function loadTerms() {
    const rows = document.getElementById("vocabRows");
    const response = await fetch(`/api/glossary?target_lang=${VOCAB_LANG}`);
    const terms = await response.json();
    rows.replaceChildren();
    terms.forEach(term => {
        const tr = document.createElement("tr");
        [term.source_term, term.target_term,
         term.match_case ? "yes" : "", term.enabled ? "yes" : ""].forEach(value => {
            const td = document.createElement("td");
            td.textContent = value;
            tr.appendChild(td);
        });
        const actions = document.createElement("td");
        const remove = document.createElement("button");
        remove.type = "button";
        remove.className = "vocab-delete";
        // These rows are built after load, so the button cannot be wired from
        // the template. The id rides on a data attribute and the click is
        // picked up by the delegated listener below.
        remove.dataset.termId = term.id;
        remove.textContent = "Delete";
        actions.appendChild(remove);
        tr.appendChild(actions);
        rows.appendChild(tr);
    });
}

async function addTerm() {
    showVocabError("");
    const response = await fetch("/api/glossary", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({
            target_lang: VOCAB_LANG,
            source_term: document.getElementById("newSource").value,
            target_term: document.getElementById("newTarget").value,
            match_case: document.getElementById("newMatchCase").checked,
            enabled: document.getElementById("newEnabled").checked
        })
    });
    if (!response.ok) {
        const body = await response.json().catch(() => ({}));
        showVocabError(typeof body.detail === "string" ? body.detail : "Could not add term");
        return;
    }
    document.getElementById("newSource").value = "";
    document.getElementById("newTarget").value = "";
    loadTerms();
}

async function deleteTerm(id) {
    await fetch(`/api/glossary/${id}`, {method: "DELETE"});
    loadTerms();
}

async function importTerms() {
    showVocabError("");
    const input = document.getElementById("vocabImport");
    if (!input.files.length) { return; }
    const mode = document.getElementById("vocabReplace").checked ? "replace" : "merge";
    const form = new FormData();
    form.append("file", input.files[0]);
    const response = await fetch(
        `/api/glossary/import?target_lang=${VOCAB_LANG}&mode=${mode}`,
        {method: "POST", body: form}
    );
    const body = await response.json();
    if (body.errors && body.errors.length) {
        showVocabError(`${body.imported} imported, ${body.skipped} skipped: ${body.errors[0]}`);
    }
    input.value = "";
    loadTerms();
}

document.getElementById("addTermBtn").addEventListener("click", addTerm);
document.getElementById("vocabImport").addEventListener("change", importTerms);
// Delegated: loadTerms() replaces the whole tbody on every render, so listening
// on the container covers rows that do not exist yet and needs no re-binding.
document.getElementById("vocabRows").addEventListener("click", event => {
    const button = event.target.closest("button.vocab-delete");
    if (button) {
        deleteTerm(button.dataset.termId);
    }
});

loadTerms();

// --- Recent Translations (issue #28) ---------------------------------------
// Moved out of an inline <script> inside #historySection. Kept in an IIFE so
// its helpers stay out of the shared global scope.
(function () {
    const listEl = document.getElementById("historyList");
    const retentionEl = document.getElementById("historyRetention");

    const LABELS = {
        processing: "Translating…",
        expired: "Expired",
        unavailable: "File no longer available",
        failed: "Translation failed",
        cancelled: "Cancelled",
        interrupted: "Interrupted"
    };

    function formatSize(bytes) {
        if (bytes === null || bytes === undefined) return "";
        if (bytes < 1024) return bytes + " B";
        if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + " KB";
        return (bytes / (1024 * 1024)).toFixed(1) + " MB";
    }

    function formatDate(value) {
        if (!value) return "";
        const parsed = new Date(value);
        return isNaN(parsed) ? value : parsed.toLocaleString();
    }

    function metaText(entry) {
        const parts = [formatDate(entry.created_at)];
        if (entry.source_language && entry.target_language) {
            parts.push(entry.source_language + " → " + entry.target_language);
        }
        const size = formatSize(entry.file_size);
        if (size) parts.push(size);
        if (entry.status === "available" && entry.expires_at) {
            parts.push("expires " + formatDate(entry.expires_at));
        }
        return parts.join(" · ");
    }

    function renderEntry(entry) {
        const item = document.createElement("li");

        const name = document.createElement("span");
        name.className = "history-name";
        name.textContent = entry.original_filename || entry.translated_filename || "Untitled";
        item.appendChild(name);

        const meta = document.createElement("span");
        meta.className = "history-meta";
        meta.textContent = metaText(entry);
        item.appendChild(meta);

        const action = document.createElement("span");
        action.className = "history-action";
        if (entry.status === "available" && entry.download_url) {
            const link = document.createElement("a");
            link.href = entry.download_url;
            link.setAttribute("download", "");
            link.textContent = "Download";
            action.appendChild(link);
        } else {
            const state = document.createElement("span");
            state.className = "history-state";
            if (entry.status === "failed" || entry.status === "interrupted") {
                state.className += " history-failed";
            }
            state.textContent = LABELS[entry.status] || entry.status;
            action.appendChild(state);
        }
        item.appendChild(action);

        return item;
    }

    function render(data) {
        const entries = (data && data.entries) || [];
        if (data && data.retention_days) {
            retentionEl.textContent = "Files are kept for " + data.retention_days + " days.";
        }
        listEl.textContent = "";
        if (!entries.length) {
            const empty = document.createElement("li");
            empty.className = "history-empty";
            empty.textContent = "No translations yet.";
            listEl.appendChild(empty);
            return;
        }
        entries.forEach(function (entry) {
            listEl.appendChild(renderEntry(entry));
        });
    }

    function refreshHistory() {
        fetch("/api/history", { credentials: "same-origin" })
            .then(function (response) { return response.json(); })
            .then(render)
            .catch(function (error) {
                console.error("History load error:", error);
            });
    }

    document.getElementById("historyRefresh").addEventListener("click", refreshHistory);
    setInterval(function () {
        if (document.visibilityState !== "hidden") refreshHistory();
    }, 15000);
    refreshHistory();
})();
