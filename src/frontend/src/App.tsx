import './App.css';
import { BrowserRouter as Router, Routes, Route, Navigate } from 'react-router-dom';
import { Provider } from 'react-redux';
import store from './store';
import { PlanPage } from './pages';
import { useWebSocket } from './hooks/useWebSocket';

function App() {
    useWebSocket();

  return (
    <Provider store={store}>
      <Router>
        <Routes>
          <Route path="/" element={<PlanPage />} />
          <Route path="/session/:sessionId" element={<PlanPage />} />
          <Route path="/plan/:planId" element={<PlanPage />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </Router>
    </Provider>
  );
}

export default App;
