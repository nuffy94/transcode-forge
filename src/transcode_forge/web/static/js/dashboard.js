/* Dashboard page: pause control and live progress. Running jobs carry no
 * cancel action (the cancel endpoint accepts waiting jobs only).
 */

import { initPauseButton, initLiveProgress } from './ops.js';

initPauseButton();
initLiveProgress();
