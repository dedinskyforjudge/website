window.formspree = window.formspree || function () { (formspree.q = formspree.q || []).push(arguments); };
// useDefaultStyles:false — the SDK's injected stylesheet would otherwise
// out-specificity our .form-success/.form-error styling; visibility rules
// for [data-fs-active] live in style.css instead.
formspree('initForm', { formElement: '#support-form', formId: 'mojyzvpo', useDefaultStyles: false });
