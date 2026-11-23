const { chromium } = require('playwright');
(async()=>{
 const browser=await chromium.launch({executablePath:process.env.CHROMIUM_PATH||'/usr/bin/chromium',headless:true,args:['--no-sandbox','--disable-dev-shm-usage']});
 const page=await browser.newPage();
 const errors=[];
 page.on('pageerror',e=>errors.push(e.message));
 page.on('response',r=>{if(r.url().includes('/api/')&&r.status()>=400 && r.status()!==401) errors.push(r.status()+' '+new URL(r.url()).pathname)});
 const base=process.env.APP_URL||'http://localhost:8000';
 await page.goto(base+'/api/auth/google');
 await page.waitForURL(base+'/');
 for(const path of ['/','/empresas','/contactos','/descubrimiento','/segmentos','/propuestas','/secuencias','/campanas','/respuestas','/embudo','/informes','/configuracion','/auditoria']) {
   await page.goto(base+path);
   await page.waitForLoadState('networkidle');
   const body=await page.locator('body').innerText();
   if(body.includes('La API devolvió datos con un formato inesperado')||body.includes('No fue posible completar la solicitud')) errors.push('Error visible en '+path);
   if(!body.includes('NEXUS')) errors.push('La aplicación no abrió '+path);
   // Propuestas ya no es una pantalla: lleva a Secuencias de correo.
   if(path==='/propuestas' && new URL(page.url()).pathname!=='/secuencias') errors.push('/propuestas no llevó a /secuencias');
   console.log('Pantalla '+path+' cargada');
 }
 await page.goto(base+'/empresas');
 await page.waitForLoadState('networkidle');
 const buttons=await page.getByRole('button').allTextContents();
 console.log('Botones empresas: '+buttons.join(', '));
 await browser.close();
 if(errors.length) throw new Error(errors.join('\n'));
 console.log('13 pantallas reales sin errores de navegador ni de contrato');
})().catch(e=>{console.error(e);process.exit(1)});
