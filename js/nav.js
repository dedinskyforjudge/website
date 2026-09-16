// Mobile nav toggle
document.addEventListener('DOMContentLoaded', function() {
  var skipLink = document.querySelector('.skip-link[href="#main"]');
  var main = document.querySelector('main#main');
  if (skipLink && main) {
    skipLink.addEventListener('click', function(event) {
      event.preventDefault();
      document.body.classList.add('is-scrolled');
      main.setAttribute('tabindex', '-1');
      main.focus({ preventScroll: true });
      main.scrollIntoView({ behavior: 'auto', block: 'start' });
      window.history.pushState(null, '', '#main');
    });
  }

  var toggle = document.querySelector('.nav-toggle');
  var links = document.querySelector('.nav-links');
  if (toggle && links) {
    function setOpen(open) {
      links.classList.toggle('open', open);
      toggle.textContent = open ? '✕' : '☰';
      toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
    }
    toggle.addEventListener('click', function() {
      setOpen(!links.classList.contains('open'));
    });
    links.querySelectorAll('a').forEach(function(a) {
      a.addEventListener('click', function() { setOpen(false); });
    });
    document.addEventListener('keydown', function(e) {
      if (e.key === 'Escape' && links.classList.contains('open')) {
        setOpen(false);
        toggle.focus();
      }
    });
  }

  // Shrinking nav on scroll. Hysteresis: shrinking removes 54px of document
  // height, so a single threshold could oscillate on pages barely taller
  // than the viewport — shrink past 64, only grow back under 10.
  var ticking = false;
  function update() {
    var y = window.scrollY;
    if (y > 64) {
      document.body.classList.add('is-scrolled');
    } else if (y < 10) {
      document.body.classList.remove('is-scrolled');
    }
    ticking = false;
  }
  window.addEventListener('scroll', function() {
    if (!ticking) {
      window.requestAnimationFrame(update);
      ticking = true;
    }
  }, { passive: true });
  update();
});
