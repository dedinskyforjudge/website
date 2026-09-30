// Event page RSVP (SPEC S13.7). Touch vs mouse comes from the primary pointer, not the
// screen width: touch leads with the mail app, a mouse leads with copying the address.
// Loaded at the end of <body>, so the elements below already exist.
(function() {
  var touch = window.matchMedia('(hover: none) and (pointer: coarse)').matches;
  document.documentElement.classList.add('js', touch ? 'touch' : 'mouse');

  // On touch, the details card's call to action opens mail directly; with a mouse it
  // keeps its #rsvp link and scrolls to the RSVP band.
  if (touch) {
    document.querySelectorAll('[data-touch-href]').forEach(function(a) {
      a.href = a.getAttribute('data-touch-href');
    });
  }

  var copy = document.querySelector('.reply .copy');
  var addr = document.getElementById('addr');
  var status = document.querySelector('.reply .status');
  if (!copy || !addr || !status) return;

  function selectAddress() {
    var range = document.createRange();
    range.selectNodeContents(addr);
    var sel = window.getSelection();
    sel.removeAllRanges();
    sel.addRange(range);
    status.textContent = 'Address selected. Press Ctrl+C (or ⌘C) to copy.';
  }

  copy.addEventListener('click', function() {
    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(addr.textContent).then(function() {
        status.textContent = 'Copied ✓';
      }, selectAddress);
    } else {
      selectAddress();
    }
  });
})();
