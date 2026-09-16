window.formspree = window.formspree || function () { (formspree.q = formspree.q || []).push(arguments); };
// useDefaultStyles:false — the SDK's injected stylesheet would otherwise
// out-specificity our .form-success/.form-error styling; visibility rules
// for [data-fs-active] live in style.css instead.
formspree('initForm', { formElement: '#support-form', formId: 'mojyzvpo', useDefaultStyles: false });

const supportForm = document.querySelector('#support-form');

if (supportForm) {
  const feedbackRoot = supportForm.parentElement;
  let submissionPending = false;
  let feedbackCheckQueued = false;

  const focusAndReveal = (element) => {
    element.focus({ preventScroll: true });
    element.scrollIntoView({ behavior: 'auto', block: 'center' });
  };

  const revealSubmissionFeedback = () => {
    feedbackCheckQueued = false;
    if (!submissionPending) return;

    const fieldError = feedbackRoot.querySelector('[data-fs-error][data-fs-active]:not([data-fs-error=""])');
    if (fieldError) {
      const field = supportForm.elements.namedItem(fieldError.dataset.fsError);
      if (field instanceof HTMLElement) {
        submissionPending = false;
        focusAndReveal(field);
        return;
      }
    }

    const status = feedbackRoot.querySelector('[data-fs-success][data-fs-active], [data-fs-error=""][data-fs-active]');
    if (status) {
      submissionPending = false;
      focusAndReveal(status);
    }
  };

  supportForm.addEventListener('submit', () => {
    submissionPending = true;
  });

  new MutationObserver(() => {
    if (!feedbackCheckQueued) {
      feedbackCheckQueued = true;
      queueMicrotask(revealSubmissionFeedback);
    }
  }).observe(feedbackRoot, {
    attributes: true,
    attributeFilter: ['data-fs-active'],
    childList: true,
    characterData: true,
    subtree: true
  });
}
