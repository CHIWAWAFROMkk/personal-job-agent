import { elements } from './elements.js';
import * as state from './state.js';
import * as api from './api.js';
import { escapeHtml, formatDateTime, numberText, safeExternalUrl, joinValues } from './utils.js';
import { statusLabels, trackLabels, strategyFitLabels, priorityLabels, commuteLabels } from './labels.js';
import * as main from './main.js';

// Bind to main functions
const { showError, showSuccess, setLoading, compensationText, commuteText, statusBadgeClass, strategyBadgeClass, priorityBadgeClass, commuteBadgeClass, loadDashboard, navigate } = main;
// Variables
const composerDrafts = main.composerDrafts || new Map();
const $ = (id) => document.getElementById(id);


