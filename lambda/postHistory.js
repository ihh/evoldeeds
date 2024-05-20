import fs from 'fs';
import { makeCigarTree } from '../cigartree.js';

if (process.argv.length != 5) {
    console.error('Usage: ' + process.argv[1] + ' family_id tree.nh align.fa');
    process.exit(1);
}

const url = 'https://api.evoldeeds.com/histories/';

const [ familyId, treeFilename, alignFilename ] = process.argv.slice(2);
const treeStr = fs.readFileSync(treeFilename).toString();
const alignStr = fs.readFileSync(alignFilename).toString();

const ct = makeCigarTree (treeStr, alignStr, { forceLowerCase: true, omitSeqs: true });
console.warn(JSON.stringify(ct));

const post = async (id, history) => {
    const response = await fetch(url + id, {
        method: "POST",
        mode: "cors",
        // credentials: "same-origin", // include, *same-origin, omit
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify(history),
      });
      console.log (JSON.stringify(response.json()));
};

post (familyId, ct);

