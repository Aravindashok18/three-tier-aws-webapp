const form = document.getElementById("note-form");
const list = document.getElementById("notes");
const errorBox = document.getElementById("form-error");

function showError(message) {
  errorBox.textContent = message;
  errorBox.hidden = !message;
}

function renderNote(note) {
  const item = document.createElement("li");
  item.dataset.id = note.id;

  const text = document.createElement("p");
  text.textContent = note.body; // textContent, never innerHTML: notes are user input

  const footer = document.createElement("footer");
  const meta = document.createElement("span");
  meta.textContent = `${note.author} · ${note.created_at}`;
  const del = document.createElement("button");
  del.className = "delete";
  del.type = "button";
  del.setAttribute("aria-label", "Delete note");
  del.textContent = "Delete";

  footer.append(meta, del);
  item.append(text, footer);
  return item;
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  showError("");
  const data = Object.fromEntries(new FormData(form));
  const response = await fetch("/api/notes", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(data),
  });
  const body = await response.json();
  if (!response.ok) {
    showError(body.error || "Could not save the note");
    return;
  }
  list.querySelector(".empty")?.remove();
  list.prepend(renderNote(body));
  form.elements.body.value = "";
});

list.addEventListener("click", async (event) => {
  if (!event.target.classList.contains("delete")) return;
  const item = event.target.closest("li");
  const response = await fetch(`/api/notes/${item.dataset.id}`, { method: "DELETE" });
  if (response.ok) item.remove();
});
