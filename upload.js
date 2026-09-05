const { google } = require('googleapis');
const fs = require('fs');
const path = require('path');
const { DatabaseSync } = require('node:sqlite');

const ROOT = __dirname;
const DB_PATH = path.join(ROOT, 'state', 'app.db');

const DEFAULT_TITLE = 'Discipline Builds You | AI Motivation #Shorts #motivation';
const DEFAULT_DESCRIPTION = `### Description (YouTube / Reels / Shorts)

Unlock your full potential with powerful AI-driven motivation. This video is built to push you beyond limits, cut through distractions, and reprogram your mindset for success. Whether you're grinding late nights, building your dream, staying disciplined in the gym, or forcing yourself to keep going when it's uncomfortable, this is your fuel.

Real progress comes from consistency, not emotion. Motivation starts the fire, but discipline keeps it burning. Train your mind to stay focused, ignore the noise, and execute daily. No excuses. No shortcuts. No waiting for the perfect moment.

This AI-generated motivation content blends cinematic visuals, intense pacing, and mindset-shifting energy to help you stay locked in. Use it when you're tired. Use it when you're distracted. Use it when you're tempted to quit. Rewire your habits. Sharpen your focus. Become unstoppable.

Success is not luck. It is discipline, clarity, sacrifice, and relentless execution stacked day after day.

If this hits, come back tomorrow and run it again.

---

### Hashtags

#motivation #motivationdaily #selfdiscipline #discipline #grindset #successmindset #winnermindset #stayhard #focus #consistency
#noexcuses #mindsetshift #workethic #disciplineequalsfreedom #mentalstrength #peakperformance #entrepreneur #hustle #dailygrind
#nevergiveup #goals #selfimprovement #productivity #riseandgrind #dreambig #successquotes #motivationalvideo #ai #aimotivation
#aivideo #cinematicvideo #viralvideo #shorts #reels #explorepage #fyp #trendingnow #gymmotivation #studyhard #lategrind
`;
const DEFAULT_TAGS = [
  'motivation',
  'motivation daily',
  'self discipline',
  'discipline',
  'grindset',
  'success mindset',
  'focus',
  'consistency',
  'no excuses',
  'mental strength',
  'self improvement',
  'ai motivation',
  'ai video',
  'cinematic motivation',
  'shorts',
  'motivational video',
];
const DEFAULT_PRIVACY = 'public';
const DEFAULT_CATEGORY_ID = '22';

function parseArgs(argv) {
  const args = {};
  for (let i = 2; i < argv.length; i += 1) {
    const current = argv[i];
    if (current === '--file') {
      args.file = argv[++i];
    } else if (current === '--title') {
      args.title = argv[++i];
    } else if (current === '--privacy') {
      args.privacy = argv[++i];
    } else if (current === '--description') {
      args.description = argv[++i];
    } else if (current === '--tags') {
      args.tags = argv[++i];
    } else if (current === '--category') {
      args.category = argv[++i];
    } else if (current === '--json') {
      args.json = true;
    }
  }
  return args;
}

function jobRowForFile(resolvedVideo) {
  const db = new DatabaseSync(DB_PATH, { readonly: true });
  try {
    const relativeFromRoot = path.relative(ROOT, resolvedVideo).replaceAll(path.sep, '/');
    const row = db.prepare(`
      SELECT id, output_path, quote, author, completed_at
      FROM jobs
      WHERE status = 'completed' AND output_path = ?
      ORDER BY completed_at DESC, id DESC
      LIMIT 1
    `).get(relativeFromRoot);
    return row || null;
  } finally {
    db.close();
  }
}

function newestCompletedVideoFromDb() {
  const db = new DatabaseSync(DB_PATH, { readonly: true });
  try {
    const row = db.prepare(`
      SELECT id, output_path, quote, author, completed_at
      FROM jobs
      WHERE status = 'completed' AND output_path IS NOT NULL AND output_path != ''
      ORDER BY completed_at DESC, id DESC
      LIMIT 1
    `).get();
    if (!row) {
      throw new Error('No completed job-backed output found in state/app.db');
    }
    return row;
  } finally {
    db.close();
  }
}

function requireValue(name, value) {
  if (!value || !String(value).trim()) {
    throw new Error(`Missing required value: ${name}`);
  }
  return String(value).trim();
}

async function main() {
  const args = parseArgs(process.argv);
  const jsonMode = Boolean(args.json);
  const clientId = process.env.YOUTUBE_CLIENT_ID;
  const clientSecret = process.env.YOUTUBE_CLIENT_SECRET;
  const refreshToken = process.env.YOUTUBE_REFRESH_TOKEN;
  requireValue('YOUTUBE_CLIENT_ID', clientId);
  requireValue('YOUTUBE_CLIENT_SECRET', clientSecret);
  requireValue('YOUTUBE_REFRESH_TOKEN', refreshToken);

  const latest = newestCompletedVideoFromDb();
  const selectedRelative = args.file || process.env.YOUTUBE_VIDEO_FILE || latest.output_path;
  const resolvedVideo = path.resolve(ROOT, selectedRelative);
  if (!fs.existsSync(resolvedVideo)) {
    throw new Error(`Video file not found: ${resolvedVideo}`);
  }
  const sourceJob = jobRowForFile(resolvedVideo) || latest;

  const title = (args.title || process.env.YOUTUBE_TITLE || DEFAULT_TITLE).trim();
  if (title.length > 100) {
    throw new Error(`Title exceeds 100 characters (${title.length}): ${title}`);
  }
  const description = (args.description || process.env.YOUTUBE_DESCRIPTION || DEFAULT_DESCRIPTION).trim();
  const privacy = (args.privacy || process.env.YOUTUBE_PRIVACY_STATUS || DEFAULT_PRIVACY).trim();
  const categoryId = (args.category || process.env.YOUTUBE_CATEGORY_ID || DEFAULT_CATEGORY_ID).trim();
  const tags = (args.tags || process.env.YOUTUBE_TAGS || DEFAULT_TAGS.join(','))
    .split(',')
    .map((tag) => tag.trim())
    .filter(Boolean);

  const sizeMb = (fs.statSync(resolvedVideo).size / 1024 / 1024).toFixed(2);
  if (!jsonMode) {
    console.log(`\n📁 File       : ${resolvedVideo}`);
    console.log(`📦 Size       : ${sizeMb} MB`);
    console.log(`🆔 Source job : #${sourceJob.id}`);
    console.log(`🎬 Title      : ${title}`);
    console.log(`🔒 Privacy    : ${privacy}`);
    console.log(`🏷️ Tags       : ${tags.join(', ')}`);
    console.log('\n⏳ Uploading to YouTube...\n');
  }

  const oauth2Client = new google.auth.OAuth2(clientId, clientSecret, 'http://localhost');
  oauth2Client.setCredentials({ refresh_token: refreshToken });
  const youtube = google.youtube({ version: 'v3', auth: oauth2Client });

  const res = await youtube.videos.insert({
    part: ['snippet', 'status'],
    requestBody: {
      snippet: {
        title,
        description,
        tags,
        categoryId,
      },
      status: {
        privacyStatus: privacy,
        selfDeclaredMadeForKids: false,
      },
    },
    media: {
      body: fs.createReadStream(resolvedVideo),
    },
  });

  const videoId = res.data.id;
  const payload = {
    ok: true,
    videoId,
    watchUrl: `https://www.youtube.com/watch?v=${videoId}`,
    shortsUrl: `https://www.youtube.com/shorts/${videoId}`,
    title,
    privacy,
    file: resolvedVideo,
    sourceJobId: sourceJob.id,
  };
  if (jsonMode) {
    console.log(JSON.stringify(payload));
    return;
  }
  console.log('✅ Upload successful!\n');
  console.log(`🆔 Video ID  : ${videoId}`);
  console.log(`🔗 Watch URL : ${payload.watchUrl}`);
  console.log(`📱 Short URL : ${payload.shortsUrl}`);
  console.log('\nNotes:');
  console.log('- YouTube decides whether it surfaces as a Short based on format and platform behavior.');
  console.log('- This upload uses videos.insert with OAuth and public visibility.');
}

main().catch((err) => {
  const message = String(err.message || '');
  const reason = err?.errors?.[0]?.reason || err?.response?.data?.error?.errors?.[0]?.reason || '';
  const payload = {
    ok: false,
    message,
    code: err?.code || err?.response?.status || null,
    reason,
  };
  if (process.argv.includes('--json')) {
    console.log(JSON.stringify(payload));
  } else {
    console.error(`❌ Upload failed: ${message}`);
    if (/quota/i.test(message) || /quota/i.test(reason)) {
      console.error('   Daily YouTube Data API quota may be exhausted.');
    }
    if (/forbidden|403/i.test(message)) {
      console.error('   Check that YouTube Data API v3 is enabled and the OAuth project/channel is allowed to upload.');
    }
    if (/invalid_grant|refresh token|unauthorized/i.test(message)) {
      console.error('   The refresh token may be invalid or revoked.');
    }
  }
  process.exit(1);
});
