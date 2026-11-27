(() => {
  const control = document.getElementById('displayLanguage');
  const labels = {
    '#caregiver': ['\u0c86\u0cb0\u0cc8\u0c95\u0cc6\u0ca6\u0cbe\u0cb0', '\u0926\u0947\u0916\u092d\u093e\u0932\u0915\u0930\u094d\u0924\u093e'],
    '#devices': ['\u0cb8\u0cbe\u0ca7\u0ca8\u0c97\u0cb3\u0cc1', '\u0909\u092a\u0915\u0930\u0923'],
    '#intelligence': ['AI \u0cb5\u0cbf\u0cb6\u0ccd\u0cb2\u0cc7\u0cb7\u0ca3\u0cc6', 'AI \u0935\u093f\u0936\u094d\u0932\u0947\u0937\u0923'],
    '#home': ['ಮನೆ', 'मुखपृष्ठ'],
    '#calibration': ['ಮಾಪನಾಂಕ', 'कैलिब्रेशन'],
    '#demo': ['ಪ್ರದರ್ಶನ', 'डेमो'],
    '#connection': ['ಸಂಪರ್ಕ', 'कनेक्ट'],
    '#analytics': ['ವರದಿಗಳು', 'रिपोर्ट'],
    '#family': ['ಕುಟುಂಬ', 'परिवार'],
    '#research': ['ಸಂಶೋಧನೆ', 'शोध'],
    '#settings': ['ಸೆಟ್ಟಿಂಗ್‌ಗಳು', 'सेटिंग्स'],
    '#assessment-evidence': ['ಮೌಲ್ಯಮಾಪನ ಸಾಕ್ಷ್ಯ', 'आकलन के प्रमाण'],
    '#telemetry': ['ನೇರ ಮಾನಿಟರ್', 'लाइव मॉनिटर'],
  };
  const entries = [];
  for (const link of document.querySelectorAll('.navlinks a')) {
    const translations = labels[link.getAttribute('href')];
    const label = link.querySelector('.navLabel') || link;
    if (translations) entries.push([label, label.textContent, translations]);
  }
  entries.push([document.getElementById('caregiverNavLabel'), 'Notifications', labels['#caregiver']]);
  entries.push([document.getElementById('languageLabel'), 'Navigation language', ['ನ್ಯಾವಿಗೇಷನ್ ಭಾಷೆ', 'नेविगेशन की भाषा']]);
  entries.push([document.getElementById('settingsHeading'), 'Profiles and settings', ['ಪ್ರೊಫೈಲ್‌ಗಳು ಮತ್ತು ಸೆಟ್ಟಿಂಗ್‌ಗಳು', 'प्रोफ़ाइल और सेटिंग्स']]);

  function apply() {
    const language = control.value;
    for (const [node, english, translations] of entries) {
      node.textContent = language === 'en' ? english : translations[language === 'kn' ? 0 : 1];
      node.lang = language;
    }
    localStorage.setItem('sentry-navigation-language', language);
  }
  const saved = localStorage.getItem('sentry-navigation-language');
  control.value = ['kn', 'hi'].includes(saved) ? saved : 'en';
  control.onchange = apply;
  apply();
})();
