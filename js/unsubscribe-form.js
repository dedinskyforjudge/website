window.formspree = window.formspree || function () { (formspree.q = formspree.q || []).push(arguments); };
formspree('initForm', { formElement: '#unsubscribe-form', formId: 'moevbzkk', useDefaultStyles: false });

const unsubscribeForm = document.querySelector('#unsubscribe-form');

if (unsubscribeForm) {
  const feedbackRoot = unsubscribeForm.parentElement;
  const emailField = unsubscribeForm.elements.namedItem('email');
  let submissionPending = false;
  let feedbackCheckQueued = false;
  let successDismissTimer = 0;
  const SUCCESS_NOTICE_TIMEOUT = 6000;

  const clearFeedback = (element) => {
    element.removeAttribute('data-fs-active');
    if (element.hasAttribute('data-fs-server-content')) {
      element.textContent = '';
      element.removeAttribute('data-fs-server-content');
    }
  };

  const clearErrors = () => {
    feedbackRoot.querySelectorAll('[data-fs-error][data-fs-active]').forEach(clearFeedback);
  };

  const scheduleSuccessDismissal = (success) => {
    window.clearTimeout(successDismissTimer);
    successDismissTimer = window.setTimeout(() => clearFeedback(success), SUCCESS_NOTICE_TIMEOUT);
  };

  const focusAndReveal = (element) => {
    element.focus({ preventScroll: true });
    element.scrollIntoView({ behavior: 'auto', block: 'center' });
  };

  const revealSubmissionFeedback = () => {
    feedbackCheckQueued = false;
    if (!submissionPending) return;

    const fieldError = feedbackRoot.querySelector('[data-fs-error][data-fs-active]:not([data-fs-error=""])');
    if (fieldError) {
      const field = unsubscribeForm.elements.namedItem(fieldError.dataset.fsError);
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
      if (status.matches('[data-fs-success]')) scheduleSuccessDismissal(status);
    }
  };

  unsubscribeForm.addEventListener('submit', () => {
    submissionPending = true;
    window.clearTimeout(successDismissTimer);
    clearErrors();
  });

  if (emailField instanceof HTMLElement) {
    emailField.addEventListener('input', clearErrors);
  }

  feedbackRoot.addEventListener('click', (event) => {
    const closeButton = event.target.closest('[data-fs-notice-close]');
    if (!closeButton) return;
    const notice = closeButton.closest('[data-fs-notice]');
    const feedback = notice?.querySelector('[data-fs-success], [data-fs-error]');
    if (!feedback) return;
    window.clearTimeout(successDismissTimer);
    clearFeedback(feedback);
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
