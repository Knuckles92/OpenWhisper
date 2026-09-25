import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import App from './App';
import './styles.css';
import { applyDesignTheme, applyTheme, readDesignTheme, readTheme } from './theme';
import './themes/palettes.css';
import './themes/designs.css';

// Before the first paint, so a pinned theme never flashes the other one.
applyTheme(readTheme());
applyDesignTheme(readDesignTheme());

const root = document.getElementById('root');
if (root) {
  createRoot(root).render(
    <StrictMode>
      <App />
    </StrictMode>,
  );
}
