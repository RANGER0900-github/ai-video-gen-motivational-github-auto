#!/usr/bin/env node
const { google } = require('googleapis');
const readline = require('readline');

const rl = readline.createInterface({
  input: process.stdin,
  output: process.stdout,
});

function question(prompt) {
  return new Promise((resolve) => rl.question(prompt, resolve));
}

async function main() {
  console.log('\n========================================');
  console.log('   YouTube OAuth Refresh Token Helper');
  console.log('========================================\n');

  const clientId = process.env.YOUTUBE_CLIENT_ID || (await question('Enter your YOUTUBE_CLIENT_ID: ')).trim();
  const clientSecret = process.env.YOUTUBE_CLIENT_SECRET || (await question('Enter your YOUTUBE_CLIENT_SECRET: ')).trim();

  if (!clientId || !clientSecret) {
    console.error('❌ Error: Both Client ID and Client Secret are required.');
    process.exit(1);
  }

  // Redirect URI for Desktop / Playground auth
  const redirectUri = 'https://developers.google.com/oauthplayground';
  const oauth2Client = new google.auth.OAuth2(clientId, clientSecret, redirectUri);

  const authUrl = oauth2Client.generateAuthUrl({
    access_type: 'offline',
    prompt: 'consent',
    scope: ['https://www.googleapis.com/auth/youtube.upload'],
  });

  console.log('\n1. Open this URL in your browser to sign in to your YouTube account:');
  console.log('------------------------------------------------------------------');
  console.log(authUrl);
  console.log('------------------------------------------------------------------');
  console.log('\n2. Allow permissions. You will be redirected to OAuth Playground.');
  console.log('3. Copy the "code" query parameter from the URL bar (or code on page).\n');

  const code = (await question('Paste the authorization code here: ')).trim();
  rl.close();

  if (!code) {
    console.error('❌ Error: Authorization code cannot be empty.');
    process.exit(1);
  }

  try {
    const { tokens } = await oauth2Client.getToken(code);
    console.log('\n========================================');
    console.log('       🎉 Success! Your Tokens:');
    console.log('========================================\n');
    console.log(`export YOUTUBE_CLIENT_ID="${clientId}"`);
    console.log(`export YOUTUBE_CLIENT_SECRET="${clientSecret}"`);
    console.log(`export YOUTUBE_REFRESH_TOKEN="${tokens.refresh_token}"`);
    console.log('\nAdd these to your environment or GitHub Secrets.\n');
  } catch (err) {
    console.error('❌ Failed to retrieve tokens:', err.message);
  }
}

main().catch(console.error);
